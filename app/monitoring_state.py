from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any


logger = logging.getLogger(__name__)
MAX_SEEN_PER_CHANNEL = 500


class MonitoringState:
    """Persistent 'what we've already seen' store — video ids we already enqueued or skipped."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()
        self._seen_by_channel: dict[str, deque[str]] = {}

    def load(self) -> None:
        if not self._path.exists():
            self._seen_by_channel = {}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("monitoring.state.load_failed path=%s error=%s", self._path, exc)
            self._seen_by_channel = {}
            return

        channels_raw = data.get("channels") or {}
        parsed: dict[str, deque[str]] = {}
        for channel_id, entry in channels_raw.items():
            if isinstance(entry, dict):
                ids = entry.get("seen_video_ids") or []
            elif isinstance(entry, list):
                ids = entry
            else:
                continue
            parsed[str(channel_id)] = deque(
                (str(video_id) for video_id in ids if str(video_id).strip()),
                maxlen=MAX_SEEN_PER_CHANNEL,
            )
        self._seen_by_channel = parsed
        logger.info("monitoring.state.loaded path=%s channels=%s", self._path, len(parsed))

    def save(self) -> None:
        with self._lock:
            payload: dict[str, Any] = {
                "channels": {
                    channel_id: {"seen_video_ids": list(ids)}
                    for channel_id, ids in self._seen_by_channel.items()
                },
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)

    def is_seen(self, channel_id: str, video_id: str) -> bool:
        with self._lock:
            ids = self._seen_by_channel.get(channel_id)
            return ids is not None and video_id in ids

    def mark_seen(self, channel_id: str, video_id: str) -> None:
        with self._lock:
            ids = self._seen_by_channel.setdefault(
                channel_id, deque(maxlen=MAX_SEEN_PER_CHANNEL)
            )
            if video_id in ids:
                return
            ids.append(video_id)

    def prime_channel(self, channel_id: str, video_ids: list[str]) -> None:
        """Seed a fresh channel with the current RSS contents so the first scan doesn't
        drown the user in back-catalogue summaries."""
        with self._lock:
            bucket = self._seen_by_channel.setdefault(
                channel_id, deque(maxlen=MAX_SEEN_PER_CHANNEL)
            )
            for video_id in video_ids:
                if video_id and video_id not in bucket:
                    bucket.append(video_id)
