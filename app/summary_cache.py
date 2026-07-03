"""Persistent cache of completed YouTube summaries, keyed by video_id.

When a user re-sends an already-processed link, the bot returns the saved
summary instantly — no LLM round-trips, no transcription, no re-publishing
to Telegra.ph (we already have the page URL). The cache stores enough
structure (overview, key_points, chapters) to reconstruct the summary
later. The transcript itself is never persisted — it lives in memory only
for the duration of summary generation.

Storage layout: a single ``data/summary_cache.json`` mapping
``video_id -> dict``. Atomic write via ``tmp + replace``. Concurrent
``put``/``get`` calls are safe (process-local lock).

Design choices:
- We cache only **full-video** summaries. Segment-mode (scheduled with
  ``segment_spans``) jobs bypass cache because their output is specific
  to a particular expert window — caching them as the canonical summary
  would mislead later requesters.
- No TTL. Telegra.ph pages don't expire; videos may be unpublished from
  YouTube but the saved summary remains valuable. If a user wants a fresh
  summary, they can drop the entry manually (rm/edit cache file).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.db import Database, retire_legacy_json
from app.models import Chapter, Summary, SummaryTags, VideoComment


logger = logging.getLogger(__name__)


def _seconds_in_days(days: int) -> int:
    return max(0, days) * 86400


@dataclass
class CachedSummary:
    """One persisted summary entry — round-trip serialisable to JSON."""

    video_id: str
    url: str
    title: str
    channel_name: str
    channel_url: str
    summary_overview: str
    summary_key_points: list[str]
    summary_chapters: list[dict]   # serialized Chapter list (start/title/notes)
    summary_raw_text: str
    telegraph_url: str
    # Back-compat only: old cache entries may still have a Telegra.ph
    # transcript-page URL here. New entries always write None — transcript
    # pages are no longer published.
    transcript_url: str | None
    transcript_source: str           # "youtube" / "groq" / "whisper" / "none"
    model: str
    created_at_iso: str
    created_at_unix: float
    transcript_chars: int = 0        # for nicer status display on cache hit
    # Top YouTube comments at the time of summary generation. Stored as a list
    # of plain dicts so JSON-serialization is straightforward; reconstruct via
    # ``to_top_comments()`` when rendering.
    top_comments: list[dict] = field(default_factory=list)
    # Hashtag bundle from canonicalized SummaryTags. Stored flat so JSON
    # round-trip is trivial; reconstruct via ``tags_obj()``.
    tag_topic: str = ""
    tag_speakers: list[str] = field(default_factory=list)
    tag_hosts: list[str] = field(default_factory=list)
    tag_format: str = ""
    tag_channel: str = ""

    def to_summary(self) -> Summary:
        """Reconstruct the in-memory Summary dataclass for downstream code."""
        return Summary(
            overview=self.summary_overview,
            key_points=list(self.summary_key_points),
            chapters=[
                Chapter(
                    start=str(ch.get("start", "")),
                    title=str(ch.get("title", "")),
                    notes=str(ch.get("notes", "")),
                )
                for ch in self.summary_chapters
                if isinstance(ch, dict)
            ],
            raw_text=self.summary_raw_text,
            tags=self.tags_obj(),
        )

    def tags_obj(self) -> SummaryTags:
        """Reconstruct the SummaryTags dataclass from stored flat fields."""
        return SummaryTags(
            topic=self.tag_topic,
            speakers=tuple(s for s in self.tag_speakers if s),
            hosts=tuple(h for h in self.tag_hosts if h),
            format=self.tag_format,
            channel=self.tag_channel,
        )

    def to_top_comments(self) -> list[VideoComment]:
        """Reconstruct VideoComment instances from the JSON-stored dicts."""
        result: list[VideoComment] = []
        for item in self.top_comments:
            if not isinstance(item, dict):
                continue
            try:
                result.append(
                    VideoComment(
                        text=str(item.get("text") or ""),
                        author=str(item.get("author") or ""),
                        like_count=int(item.get("like_count") or 0),
                        is_pinned=bool(item.get("is_pinned")),
                    )
                )
            except (TypeError, ValueError):
                continue
        return result


class SummaryCache:
    """Кэш готовых саммари поверх SQLite (таблица ``summary_cache``).

    Запись хранится как JSON-payload (asdict(CachedSummary)) — схема записи
    остаётся гибкой, отдельная колонка только у created_at_unix для TTL-чисток
    на SQL-уровне.
    """

    def __init__(self, db: Database, ttl_days: int = 100, legacy_json_path: Path | None = None) -> None:
        self._db = db
        self._ttl_seconds = _seconds_in_days(ttl_days)
        if legacy_json_path is not None:
            self._migrate_legacy(legacy_json_path)
        self._cleanup_expired_at_startup()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def _migrate_legacy(self, path: Path) -> None:
        if not path.exists():
            return
        row = self._db.query_one("SELECT COUNT(*) AS n FROM summary_cache")
        if row and int(row["n"]) > 0:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("summary_cache.migrate.load_failed path=%s", path)
            return
        if not isinstance(raw, dict):
            return
        imported = 0
        for vid, body in raw.items():
            if not isinstance(body, dict):
                continue
            try:
                entry = CachedSummary(**body)
            except (TypeError, KeyError) as exc:
                logger.warning("summary_cache.migrate.skip video_id=%s error=%s", vid, exc)
                continue
            self.put(entry)
            imported += 1
        retire_legacy_json(path)
        logger.info("summary_cache.migrated entries=%s", imported)

    def _cleanup_expired_at_startup(self) -> None:
        if self._ttl_seconds <= 0:
            return
        cutoff = time.time() - self._ttl_seconds
        self._db.execute("DELETE FROM summary_cache WHERE created_at_unix < ?", (cutoff,))

    def get(self, video_id: str) -> CachedSummary | None:
        row = self._db.query_one(
            "SELECT payload, created_at_unix FROM summary_cache WHERE video_id = ?", (video_id,)
        )
        if row is None:
            return None
        if self._ttl_seconds > 0 and (time.time() - row["created_at_unix"]) > self._ttl_seconds:
            self._db.execute("DELETE FROM summary_cache WHERE video_id = ?", (video_id,))
            logger.info("summary_cache.expired video_id=%s", video_id)
            return None
        try:
            return CachedSummary(**json.loads(row["payload"]))
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("summary_cache.corrupt_entry video_id=%s error=%s", video_id, exc)
            return None

    def put(self, entry: CachedSummary) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO summary_cache(video_id, payload, created_at_unix) VALUES (?, ?, ?)",
            (entry.video_id, json.dumps(asdict(entry), ensure_ascii=False), entry.created_at_unix),
        )
        logger.info("summary_cache.stored video_id=%s telegraph_url=%s", entry.video_id, entry.telegraph_url)

    def delete(self, video_id: str) -> bool:
        exists = self._db.query_one("SELECT 1 FROM summary_cache WHERE video_id = ?", (video_id,)) is not None
        if exists:
            self._db.execute("DELETE FROM summary_cache WHERE video_id = ?", (video_id,))
        return exists

    def size(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM summary_cache")
        return int(row["n"]) if row else 0
