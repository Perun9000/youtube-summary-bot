"""Выбор языка пользователя (таблица user_langs).

source: 'auto' — детект из Telegram language_code при первом контакте;
'manual' — явный выбор через /language (auto его больше не перезаписывает).
"""
from __future__ import annotations

import time

from app.db import Database


class UserLangStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, user_id: int) -> tuple[str, str] | None:
        row = self._db.query_one(
            "SELECT lang, source FROM user_langs WHERE user_id = ?", (user_id,)
        )
        return (str(row["lang"]), str(row["source"])) if row else None

    def set(self, user_id: int, lang: str, source: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO user_langs(user_id, lang, source, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, lang, source, time.time()),
        )
