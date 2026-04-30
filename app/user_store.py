from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AllowedUser:
    user_id: int
    name: str = ""
    added_at: str = ""


class UserStore:
    """Persistent allow-list for Telegram users.

    ``ALLOWED_USER_IDS`` remains a bootstrap seed for the first run and for
    simple manual backfills. The live source of truth is ``/data/users.json``.
    """

    def __init__(
        self,
        path: Path,
        seed_user_ids: set[int],
        owner_user_id: int | None,
    ) -> None:
        self._path = path
        self._owner_user_id = owner_user_id
        self._lock = threading.Lock()
        self._users: dict[int, AllowedUser] = {}

        self._load()
        self._seed(seed_user_ids)

    @property
    def owner_user_id(self) -> int | None:
        return self._owner_user_id

    def is_owner(self, user_id: int | None) -> bool:
        return (
            user_id is not None
            and self._owner_user_id is not None
            and user_id == self._owner_user_id
        )

    def is_allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        if self.is_owner(user_id):
            return True
        with self._lock:
            if not self._users and self._owner_user_id is None:
                return True
            return user_id in self._users

    def list_users(self) -> list[AllowedUser]:
        with self._lock:
            return sorted(self._users.values(), key=lambda user: user.user_id)

    def add_user(self, user_id: int, name: str = "") -> bool:
        name = name.strip()
        with self._lock:
            existing = self._users.get(user_id)
            if existing is not None and existing.name == name:
                return False

            self._users[user_id] = AllowedUser(
                user_id=user_id,
                name=name,
                added_at=existing.added_at if existing else _now_iso(),
            )
            self._save_locked()
            return existing is None

    def remove_user(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            raise ValueError("Нельзя удалить владельца бота.")

        with self._lock:
            if user_id not in self._users:
                return False
            del self._users[user_id]
            self._save_locked()
            return True

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            users = raw.get("users", []) if isinstance(raw, dict) else []
            loaded: dict[int, AllowedUser] = {}
            for item in users:
                user = _parse_user(item)
                if user is not None:
                    loaded[user.user_id] = user
            self._users = loaded
            logger.info("users.load.done path=%s count=%s", self._path, len(loaded))
        except Exception:
            logger.exception("users.load.failed path=%s", self._path)

    def _seed(self, seed_user_ids: set[int]) -> None:
        should_save = False
        seed_ids = set(seed_user_ids)
        if self._owner_user_id is not None:
            seed_ids.add(self._owner_user_id)

        with self._lock:
            for user_id in seed_ids:
                if user_id not in self._users:
                    self._users[user_id] = AllowedUser(
                        user_id=user_id,
                        name="owner" if user_id == self._owner_user_id else "",
                        added_at=_now_iso(),
                    )
                    should_save = True
            if should_save or not self._path.exists():
                self._save_locked()

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "users": [
                {
                    "id": user.user_id,
                    "name": user.name,
                    "added_at": user.added_at,
                }
                for user in sorted(self._users.values(), key=lambda item: item.user_id)
            ]
        }
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self._path)
        logger.info("users.save.done path=%s count=%s", self._path, len(self._users))


def _parse_user(item: Any) -> AllowedUser | None:
    if not isinstance(item, dict):
        return None
    raw_id = item.get("id", item.get("user_id"))
    try:
        user_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    name = str(item.get("name", "")).strip()
    added_at = str(item.get("added_at", "")).strip()
    return AllowedUser(user_id=user_id, name=name, added_at=added_at)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
