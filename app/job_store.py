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
        lang: str = "ru",
    ) -> int:
        now = time.time()
        return self._db.execute_returning_rowid(
            "INSERT INTO jobs(url, chat_id, scheduled, disable_notification, title_hint, status, created_at, updated_at, lang) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (url, chat_id, int(scheduled), int(disable_notification), title_hint, now, now, lang),
        )

    def set_status(self, job_id: int, status: str) -> None:
        self._db.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), job_id),
        )

    def set_deferred(self, job_id: int, run_after: float, *, attempts: int | None = None) -> None:
        """Отложить job до момента run_after (unix-время).

        Используется для премьер: ролик ещё не вышел, вернёмся за саммари
        после release + PREMIERE_SUMMARY_DELAY_HOURS. Отложенные строки не
        попадают в pending() (их не трогает restore после рестарта) —
        их поднимает deferred-scheduler через due_deferred().

        ``attempts`` — счётчик попыток транзиентного ретрая (Q4): передаётся
        только с путей авторетрая по сетевым сбоям (см. pipeline.py), премьер-
        деферрал его не трогает — attempts там остаётся 0/не меняется.
        """
        if attempts is None:
            self._db.execute(
                "UPDATE jobs SET status = 'deferred', run_after = ?, updated_at = ? WHERE id = ?",
                (run_after, time.time(), job_id),
            )
        else:
            self._db.execute(
                "UPDATE jobs SET status = 'deferred', run_after = ?, attempts = ?, updated_at = ? "
                "WHERE id = ?",
                (run_after, attempts, time.time(), job_id),
            )

    def due_deferred(self, now: float) -> list[sqlite3.Row]:
        return self._db.query(
            "SELECT * FROM jobs WHERE status = 'deferred' AND run_after IS NOT NULL "
            "AND run_after <= ? ORDER BY id",
            (now,),
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
