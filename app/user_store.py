from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import Database, retire_legacy_json


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AllowedUser:
    user_id: int
    name: str = ""
    added_at: str = ""


class UserStore:
    """Persistent allow-list поверх SQLite (таблица ``users``).

    ``ALLOWED_USER_IDS`` — только seed при первом запуске (пустая таблица).
    ``legacy_json_path`` — путь к старому users.json: если таблица пуста,
    а файл есть — импортируем и переименовываем в .migrated.
    """

    def __init__(
        self,
        db: Database,
        seed_user_ids: set[int],
        owner_user_id: int | None,
        legacy_json_path: Path | None = None,
    ) -> None:
        self._db = db
        self._owner_user_id = owner_user_id
        if self._count() == 0:
            migrated = legacy_json_path is not None and self._migrate_legacy(legacy_json_path)
            if not migrated:
                self._seed(seed_user_ids)
        self._ensure_owner()

    def _count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def _migrate_legacy(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("users.migrate.load_failed path=%s", path)
            return False
        users = raw.get("users", []) if isinstance(raw, dict) else []
        rows = []
        for item in users:
            user = _parse_user(item)
            if user is not None:
                rows.append((user.user_id, user.name, user.added_at))
        if rows:
            self._db.executemany(
                "INSERT OR REPLACE INTO users(user_id, name, added_at) VALUES (?, ?, ?)", rows
            )
        retire_legacy_json(path)
        logger.info("users.migrated count=%s", len(rows))
        return True

    def _seed(self, seed_user_ids: set[int]) -> None:
        seed_ids = set(seed_user_ids)
        if self._owner_user_id is not None:
            seed_ids.add(self._owner_user_id)
        for user_id in seed_ids:
            self._db.execute(
                "INSERT OR IGNORE INTO users(user_id, name, added_at) VALUES (?, ?, ?)",
                (user_id, "owner" if user_id == self._owner_user_id else "", _now_iso()),
            )

    def _ensure_owner(self) -> None:
        if self._owner_user_id is None:
            return
        self._db.execute(
            "INSERT OR IGNORE INTO users(user_id, name, added_at) VALUES (?, 'owner', ?)",
            (self._owner_user_id, _now_iso()),
        )

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
        if self._owner_user_id is None and self._count() == 0:
            return True
        return self._db.query_one("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) is not None

    def list_users(self) -> list[AllowedUser]:
        rows = self._db.query("SELECT user_id, name, added_at FROM users ORDER BY user_id")
        return [AllowedUser(user_id=r["user_id"], name=r["name"], added_at=r["added_at"]) for r in rows]

    def add_user(self, user_id: int, name: str = "") -> bool:
        name = name.strip()
        existing = self._db.query_one("SELECT name FROM users WHERE user_id = ?", (user_id,))
        if existing is not None and existing["name"] == name:
            return False
        if existing is None:
            self._db.execute(
                "INSERT INTO users(user_id, name, added_at) VALUES (?, ?, ?)",
                (user_id, name, _now_iso()),
            )
            return True
        self._db.execute("UPDATE users SET name = ? WHERE user_id = ?", (name, user_id))
        return False

    def remove_user(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            raise ValueError("Нельзя удалить владельца бота.")
        if self._db.query_one("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) is None:
            return False
        self._db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return True


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
