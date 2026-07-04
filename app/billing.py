"""Биллинг: подписки Stars и квоты генераций.

Модель доступа (за флагом PUBLIC_MODE):
- allowlist (таблица users) — безлимит, этот модуль их не видит вообще;
- внешний без подписки — первые QUOTA_STARTER генераций (lifetime),
  дальше QUOTA_FREE_WEEKLY за скользящие 7 дней;
- подписчик (149 Stars/мес, автопродление) — QUOTA_SUB_MONTHLY за
  скользящие 30 дней.
Списание (charge) вызывается ТОЛЬКО после успешной генерации; кэш-хиты
и упавшие job'ы бесплатны. Тяжёлые ролики (Groq-транскрипция, ≥1 ч)
списываются с weight=2 — их себестоимость на порядок выше.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.db import Database


logger = logging.getLogger(__name__)

WEEK_SEC = 7 * 86400
MONTH_SEC = 30 * 86400


@dataclass(frozen=True)
class QuotaVerdict:
    allowed: bool
    kind: str            # 'starter' | 'free' | 'sub' | ''
    remaining: int       # сколько ЕЩЁ можно списать в текущем окне (включая запрошенный weight)
    is_subscriber: bool
    deny_reason: str = ""  # '' | 'weekly_exhausted' | 'monthly_exhausted'


class BillingStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ── подписки ──────────────────────────────────────────────────────

    def activate_subscription(self, user_id: int, until_unix: float, charge_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO subscriptions(user_id, until_unix, last_charge_id, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, until_unix, charge_id, time.time()),
        )
        logger.info(
            "billing.subscription.activated user_id=%s until_unix=%.0f charge_id=%s",
            user_id, until_unix, charge_id,
        )

    def subscription_until(self, user_id: int) -> float:
        row = self._db.query_one(
            "SELECT until_unix FROM subscriptions WHERE user_id = ?", (user_id,)
        )
        return float(row["until_unix"]) if row else 0.0

    def is_subscriber(self, user_id: int, now: float | None = None) -> bool:
        return self.subscription_until(user_id) > (now if now is not None else time.time())

    def last_charge_id(self, user_id: int) -> str:
        row = self._db.query_one(
            "SELECT last_charge_id FROM subscriptions WHERE user_id = ?", (user_id,)
        )
        return str(row["last_charge_id"]) if row else ""

    def active_subscribers_count(self, now: float | None = None) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE until_unix > ?",
            ((now if now is not None else time.time()),),
        )
        return int(row["n"]) if row else 0

    # ── расход ────────────────────────────────────────────────────────

    def record_usage(
        self, user_id: int, video_id: str, weight: int, kind: str, now: float | None = None
    ) -> None:
        self._db.execute(
            "INSERT INTO usage_events(user_id, video_id, weight, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, video_id, weight, kind, now if now is not None else time.time()),
        )

    def usage_since(self, user_id: int, since: float, kind: str | None = None) -> int:
        if kind is None:
            row = self._db.query_one(
                "SELECT COALESCE(SUM(weight), 0) AS s FROM usage_events "
                "WHERE user_id = ? AND created_at > ?",
                (user_id, since),
            )
        else:
            row = self._db.query_one(
                "SELECT COALESCE(SUM(weight), 0) AS s FROM usage_events "
                "WHERE user_id = ? AND created_at > ? AND kind = ?",
                (user_id, since, kind),
            )
        return int(row["s"]) if row else 0

    def total_usage(self, user_id: int) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(SUM(weight), 0) AS s FROM usage_events WHERE user_id = ?",
            (user_id,),
        )
        return int(row["s"]) if row else 0


class QuotaService:
    def __init__(self, store: BillingStore, *, starter: int, weekly: int, monthly: int) -> None:
        self._store = store
        self._starter = starter
        self._weekly = weekly
        self._monthly = monthly

    def check(self, user_id: int, weight: int = 1, now: float | None = None) -> QuotaVerdict:
        now = now if now is not None else time.time()
        if self._store.is_subscriber(user_id, now=now):
            used = self._store.usage_since(user_id, now - MONTH_SEC)
            remaining = max(0, self._monthly - used)
            if remaining >= weight:
                return QuotaVerdict(True, "sub", remaining, True)
            return QuotaVerdict(False, "", remaining, True, "monthly_exhausted")

        total = self._store.total_usage(user_id)
        if total + weight <= self._starter:
            return QuotaVerdict(True, "starter", self._starter - total, False)

        # Недельное окно считает только post-starter события (kind='free'):
        # сожжённый в день 1 стартовый пакет не должен блокировать
        # недельную бесплатную генерацию.
        used_week = self._store.usage_since(user_id, now - WEEK_SEC, kind="free")
        remaining = max(0, self._weekly - used_week)
        if remaining >= weight:
            return QuotaVerdict(True, "free", remaining, False)
        return QuotaVerdict(False, "", remaining, False, "weekly_exhausted")

    def charge(self, user_id: int, video_id: str, weight: int, now: float | None = None) -> None:
        """Записать расход. Kind определяется тем же правилом, что и check().

        Вызывается после успешной генерации; воркер последовательный, так что
        гонка check→charge исключена по построению.
        """
        now = now if now is not None else time.time()
        verdict = self.check(user_id, weight=weight, now=now)
        kind = verdict.kind or ("sub" if verdict.is_subscriber else "free")
        self._store.record_usage(user_id, video_id, weight, kind, now=now)
        logger.info(
            "billing.usage.charged user_id=%s video_id=%s weight=%s kind=%s",
            user_id, video_id, weight, kind,
        )
