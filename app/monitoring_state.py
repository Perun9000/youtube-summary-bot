from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app.db import Database, retire_legacy_json


logger = logging.getLogger(__name__)


class MonitoringState:
    """Persistent 'what we've already seen' поверх SQLite (таблица monitoring_seen)."""

    def __init__(self, db: Database, legacy_json_path: Path | None = None) -> None:
        self._db = db
        if legacy_json_path is not None:
            self._migrate_legacy(legacy_json_path)

    def _migrate_legacy(self, path: Path) -> None:
        if not path.exists():
            return
        row = self._db.query_one("SELECT COUNT(*) AS n FROM monitoring_seen")
        if row and int(row["n"]) > 0:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("monitoring.state.migrate_failed path=%s", path)
            return
        channels_raw = (data or {}).get("channels") or {}
        now = time.time()
        rows = []
        for channel_id, entry in channels_raw.items():
            ids = entry.get("seen_video_ids") or [] if isinstance(entry, dict) else (entry if isinstance(entry, list) else [])
            for video_id in ids:
                if str(video_id).strip():
                    rows.append((str(channel_id), str(video_id), now))
        if rows:
            self._db.executemany(
                "INSERT OR IGNORE INTO monitoring_seen(channel_id, video_id, added_at) VALUES (?, ?, ?)", rows
            )
        retire_legacy_json(path)
        logger.info("monitoring.state.migrated rows=%s", len(rows))

    def is_seen(self, channel_id: str, video_id: str) -> bool:
        return self._db.query_one(
            "SELECT 1 FROM monitoring_seen WHERE channel_id = ? AND video_id = ?", (channel_id, video_id)
        ) is not None

    def mark_seen(self, channel_id: str, video_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO monitoring_seen(channel_id, video_id, added_at) VALUES (?, ?, ?)",
            (channel_id, video_id, time.time()),
        )

    def prime_channel(self, channel_id: str, video_ids: list[str]) -> None:
        """Seed a fresh channel with the current RSS contents so the first scan doesn't
        drown the user in back-catalogue summaries."""
        now = time.time()
        self._db.executemany(
            "INSERT OR IGNORE INTO monitoring_seen(channel_id, video_id, added_at) VALUES (?, ?, ?)",
            [(channel_id, vid, now) for vid in video_ids if vid],
        )
