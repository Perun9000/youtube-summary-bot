"""Персистентная очередь summary-задач (таблица ``jobs``).

asyncio.Queue остаётся рабочим механизмом «кто следующий» в памяти; таблица —
источник восстановления после рестарта. На enqueue пишем строку, worker двигает
status, при старте бота ``restore_pending_jobs`` перечитывает queued/active и
кладёт их обратно в очередь (message=None — доставка пойдёт через bot.send_message).
"""
from __future__ import annotations

import sqlite3
import time

from app.db import Database


class JobStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        url: str,
        chat_id: int,
        *,
        scheduled: bool,
        disable_notification: bool,
        title_hint: str | None,
    ) -> int:
        now = time.time()
        return self._db.execute_returning_rowid(
            "INSERT INTO jobs(url, chat_id, scheduled, disable_notification, title_hint, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (url, chat_id, int(scheduled), int(disable_notification), title_hint, now, now),
        )

    def set_status(self, job_id: int, status: str) -> None:
        self._db.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), job_id),
        )

    def pending(self) -> list[sqlite3.Row]:
        return self._db.query(
            "SELECT * FROM jobs WHERE status IN ('queued', 'active') ORDER BY id"
        )

    def scheduled_pending_count(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE scheduled = 1 AND status IN ('queued', 'active')"
        )
        return int(row["n"]) if row else 0

    def counts_since(self, days: int) -> dict[str, int]:
        cutoff = time.time() - days * 86400
        rows = self._db.query(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE created_at >= ? GROUP BY status", (cutoff,)
        )
        return {r["status"]: int(r["n"]) for r in rows}
