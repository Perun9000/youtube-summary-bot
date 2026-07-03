"""Persistent cache of completed YouTube summaries, keyed by video_id.

When a user re-sends an already-processed link, the bot returns the saved
summary instantly — no LLM round-trips, no transcription, no re-publishing
to Telegra.ph (we already have the page URL). The cache stores enough
structure (overview, key_points, chapters) to reconstruct the summary
later, paired with the on-disk transcript file.

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
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    def __init__(self, path: Path, ttl_days: int = 100) -> None:
        """
        :param path: путь к JSON-файлу кэша
        :param ttl_days: срок жизни записи в днях. 0 — TTL выключен (кэш бессрочный).
        """
        self._path = path
        self._ttl_seconds = _seconds_in_days(ttl_days)
        self._lock = threading.Lock()
        self._entries: dict[str, CachedSummary] = {}
        self._load()
        # Подметаем устаревшие записи на старте — это и место сэкономит,
        # и устранит риск отдать пользователю саммари 6-месячной давности.
        self._cleanup_expired_at_startup()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def _is_expired(self, entry: CachedSummary, now: float | None = None) -> bool:
        if self._ttl_seconds <= 0:
            return False
        now = now if now is not None else time.time()
        return (now - entry.created_at_unix) > self._ttl_seconds

    def _cleanup_expired_at_startup(self) -> None:
        """Однократная чистка при загрузке: убираем все, у кого истёк TTL."""
        if self._ttl_seconds <= 0:
            return
        now = time.time()
        with self._lock:
            stale_ids = [
                vid for vid, entry in self._entries.items()
                if self._is_expired(entry, now)
            ]
            if not stale_ids:
                return
            for vid in stale_ids:
                del self._entries[vid]
            self._save_locked()
        logger.info(
            "summary_cache.cleanup_expired removed=%s remaining=%s ttl_days=%s",
            len(stale_ids), len(self._entries), self._ttl_seconds // 86400,
        )

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "summary_cache.load_failed path=%s error=%s", self._path, exc
            )
            return
        if not isinstance(raw, dict):
            return
        entries: dict[str, CachedSummary] = {}
        for vid, body in raw.items():
            if not isinstance(body, dict):
                continue
            try:
                entries[str(vid)] = CachedSummary(**body)
            except (TypeError, KeyError) as exc:
                logger.warning(
                    "summary_cache.skip_entry video_id=%s error=%s", vid, exc
                )
                continue
        with self._lock:
            self._entries = entries
        logger.info(
            "summary_cache.loaded path=%s entries=%s", self._path, len(entries)
        )

    def _save_locked(self) -> None:
        """Persist to disk. Must be called under self._lock."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {vid: asdict(entry) for vid, entry in self._entries.items()}
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "summary_cache.save_failed path=%s error=%s", self._path, exc
            )

    def get(self, video_id: str) -> CachedSummary | None:
        """Lazy expiry: если запись устарела по TTL — удаляем и отдаём None.

        Это значит, что протухшие записи естественно отсыпаются при доступе,
        даже между запусками контейнера (помимо стартовой чистки).
        """
        with self._lock:
            entry = self._entries.get(video_id)
            if entry is None:
                return None
            if self._is_expired(entry):
                age_days = int((time.time() - entry.created_at_unix) / 86400)
                logger.info(
                    "summary_cache.expired video_id=%s age_days=%s ttl_days=%s",
                    video_id, age_days, self._ttl_seconds // 86400,
                )
                del self._entries[video_id]
                self._save_locked()
                return None
            return entry

    def put(self, entry: CachedSummary) -> None:
        with self._lock:
            self._entries[entry.video_id] = entry
            self._save_locked()
        logger.info(
            "summary_cache.stored video_id=%s telegraph_url=%s entries=%s",
            entry.video_id, entry.telegraph_url, len(self._entries),
        )

    def delete(self, video_id: str) -> bool:
        with self._lock:
            if video_id in self._entries:
                del self._entries[video_id]
                self._save_locked()
                return True
            return False

    def size(self) -> int:
        with self._lock:
            return len(self._entries)
