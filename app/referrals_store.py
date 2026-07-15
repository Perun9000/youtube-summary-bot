"""Привязки рефералов (MGM, ступень 0 — только учёт, без наград).

Модель атрибуции — first-touch навсегда: строка пишется один раз при первом
контакте пользователя с ботом, повторные переходы её не меняют
(см. docs/superpowers/specs/2026-07-15-referral-share-step0-design.md).
"""
from __future__ import annotations

import time

from app.db import Database


class ReferralsStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def bind(self, user_id: int, referrer_id: int, video_id: str = "") -> bool:
        """Привязать user_id к referrer_id. True — записали именно сейчас.

        Self-referral и повторные привязки игнорируются (first-touch).
        """
        if user_id == referrer_id:
            return False
        if self.referrer_of(user_id) is not None:
            return False
        self._db.execute(
            "INSERT OR IGNORE INTO referrals(user_id, referrer_id, video_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, referrer_id, video_id, time.time()),
        )
        return self.referrer_of(user_id) == referrer_id

    def referrer_of(self, user_id: int) -> int | None:
        row = self._db.query_one(
            "SELECT referrer_id FROM referrals WHERE user_id = ?", (user_id,)
        )
        return int(row["referrer_id"]) if row else None
