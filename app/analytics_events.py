"""Append-only аналитика по внешним пользователям.

События (все — только для не-allowlist пользователей):
- first_start      — первый /start (detail: deep_link | organic), одно на юзера;
- quota_denied     — упёрся в лимит (detail: deny_reason);
- sub_activated    — первая оплата подписки (detail: charge_id);
- sub_renewed      — автопродление (detail: charge_id).

«Первая генерация» отдельным событием не пишется — выводится из
usage_events (MIN(created_at) по пользователю). Запись событий никогда
не должна ломать пользовательский путь: все методы глотают исключения
с warning'ом.
"""
from __future__ import annotations

import logging
import time

from app.db import Database


logger = logging.getLogger(__name__)


class AnalyticsEvents:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, user_id: int, event: str, detail: str = "", now: float | None = None) -> None:
        try:
            self._db.execute(
                "INSERT INTO analytics_events(user_id, event, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, event, detail, now if now is not None else time.time()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics.record_failed event=%s error=%s", event, exc)

    def record_first_start(self, user_id: int, source: str, now: float | None = None) -> bool:
        """Записать первый /start пользователя. Идемпотентно: повторные
        вызовы для того же user_id ничего не пишут и возвращают False."""
        try:
            row = self._db.query_one(
                "SELECT 1 FROM analytics_events WHERE user_id = ? AND event = 'first_start'",
                (user_id,),
            )
            if row is not None:
                return False
            self._db.execute(
                "INSERT INTO analytics_events(user_id, event, detail, created_at) "
                "VALUES (?, 'first_start', ?, ?)",
                (user_id, source, now if now is not None else time.time()),
            )
            logger.info("analytics.first_start user_id=%s source=%s", user_id, source)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics.first_start_failed error=%s", exc)
            return False

    def funnel(self, days: int, now: float | None = None) -> dict:
        """Воронка за последние ``days`` дней (по времени события).

        first_generations — пользователи, чья ПЕРВАЯ строка в usage_events
        попала в окно (старые пользователи с новой активностью не считаются).
        """
        now = now if now is not None else time.time()
        cutoff = now - days * 86400
        try:
            def _count(sql: str) -> int:
                row = self._db.query_one(sql, (cutoff,))
                return int(row["n"]) if row else 0

            return {
                "first_starts": _count(
                    "SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events "
                    "WHERE event = 'first_start' AND created_at > ?"
                ),
                "first_generations": _count(
                    "SELECT COUNT(*) AS n FROM ("
                    "  SELECT user_id, MIN(created_at) AS first_at FROM usage_events"
                    "  GROUP BY user_id) WHERE first_at > ?"
                ),
                "quota_denied_users": _count(
                    "SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events "
                    "WHERE event = 'quota_denied' AND created_at > ?"
                ),
                "subs_activated": _count(
                    "SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events "
                    "WHERE event = 'sub_activated' AND created_at > ?"
                ),
                "sub_renewals": _count(
                    "SELECT COUNT(*) AS n FROM analytics_events "
                    "WHERE event = 'sub_renewed' AND created_at > ?"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics.funnel_failed error=%s", exc)
            return {
                "first_starts": 0, "first_generations": 0,
                "quota_denied_users": 0, "subs_activated": 0, "sub_renewals": 0,
            }
