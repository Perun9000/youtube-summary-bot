"""Persistence for "we already published this video to the channel".

Maps ``video_id`` → ``ChannelPost`` so that the second click on
«Опубликовать в канал» edits the existing message (refresh comments)
instead of producing duplicates.

JSON file format (``data/channel_posts.json``)::

    {
        "abc123XYZ": {
            "video_id": "abc123XYZ",
            "chat_id": -1001234567890,
            "message_id": 42,
            "posted_at_unix": 1746434821.5,
            "audio_attached": true
        },
        ...
    }
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass
class ChannelPost:
    video_id: str
    chat_id: int
    message_id: int
    posted_at_unix: float
    audio_attached: bool = False


class ChannelPostsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, ChannelPost] = {}
        self._load()

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
                "channel_posts.load_failed path=%s error=%s", self._path, exc
            )
            return
        if not isinstance(raw, dict):
            return
        entries: dict[str, ChannelPost] = {}
        for vid, body in raw.items():
            if not isinstance(body, dict):
                continue
            try:
                entries[str(vid)] = ChannelPost(**body)
            except (TypeError, KeyError) as exc:
                logger.warning(
                    "channel_posts.skip_entry video_id=%s error=%s", vid, exc
                )
                continue
        with self._lock:
            self._entries = entries
        logger.info(
            "channel_posts.loaded path=%s entries=%s", self._path, len(entries)
        )

    def _save_locked(self) -> None:
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
                "channel_posts.save_failed path=%s error=%s", self._path, exc
            )

    def get(self, video_id: str) -> ChannelPost | None:
        with self._lock:
            return self._entries.get(video_id)

    def put(self, post: ChannelPost) -> None:
        with self._lock:
            self._entries[post.video_id] = post
            self._save_locked()
        logger.info(
            "channel_posts.stored video_id=%s message_id=%s entries=%s",
            post.video_id, post.message_id, len(self._entries),
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
