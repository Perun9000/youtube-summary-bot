import datetime

from app.billing import BillingStore, QuotaService, WEEK_SEC, MONTH_SEC
from app.bot_handlers import _subscription_until_from_payment
from app.db import Database


NOW = 1_000_000.0


class _FakePayment:
    def __init__(self, expiration):
        self.subscription_expiration_date = expiration


def test_subscription_until_from_payment_datetime():
    dt = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.timezone.utc)
    assert _subscription_until_from_payment(_FakePayment(dt), now=0.0) == dt.timestamp()


def test_subscription_until_from_payment_fallback_30d():
    assert _subscription_until_from_payment(_FakePayment(None), now=1000.0) == 1000.0 + 30 * 86400


def make(tmp_path, starter=3, weekly=1, monthly=30):
    store = BillingStore(Database(tmp_path / "bot.db"))
    quota = QuotaService(store, starter=starter, weekly=weekly, monthly=monthly)
    return store, quota


def test_starter_pack_then_weekly(tmp_path):
    store, quota = make(tmp_path)
    # Первые 3 генерации — стартовые.
    for i in range(3):
        v = quota.check(1, now=NOW)
        assert v.allowed and v.kind == "starter"
        assert v.remaining == 3 - i
        quota.charge(1, f"vid{i}", 1, now=NOW)
    # Стартовые кончились, недельная (1/нед) свободна.
    v = quota.check(1, now=NOW)
    assert v.allowed and v.kind == "free" and v.remaining == 1
    quota.charge(1, "vid3", 1, now=NOW)
    # Неделя занята.
    v = quota.check(1, now=NOW + 1)
    assert not v.allowed and v.deny_reason == "weekly_exhausted"
    # Через 7 дней — снова можно.
    v = quota.check(1, now=NOW + WEEK_SEC + 1)
    assert v.allowed and v.kind == "free"


def test_subscriber_monthly_limit_and_expiry(tmp_path):
    store, quota = make(tmp_path, monthly=5)
    store.activate_subscription(1, until_unix=NOW + MONTH_SEC, charge_id="ch_1")
    assert store.is_subscriber(1, now=NOW)
    assert store.last_charge_id(1) == "ch_1"
    for i in range(5):
        v = quota.check(1, now=NOW)
        assert v.allowed and v.kind == "sub" and v.remaining == 5 - i
        quota.charge(1, f"v{i}", 1, now=NOW)
    v = quota.check(1, now=NOW)
    assert not v.allowed and v.deny_reason == "monthly_exhausted" and v.is_subscriber
    # Окно скользящее: через 30 дней расход «испарился», но подписка истекла →
    # пользователь снова free (стартовые уже сожжены зачётом total_usage).
    v = quota.check(1, now=NOW + MONTH_SEC + 1)
    assert not v.is_subscriber


def test_heavy_weight_respects_remaining(tmp_path):
    store, quota = make(tmp_path, starter=1)
    quota.charge(1, "v0", 1, now=NOW)          # стартовый сожжён
    v = quota.check(1, weight=2, now=NOW)       # weekly=1 < weight=2
    assert not v.allowed and v.deny_reason == "weekly_exhausted"
    store.activate_subscription(1, until_unix=NOW + MONTH_SEC, charge_id="c")
    v = quota.check(1, weight=2, now=NOW)
    assert v.allowed and v.kind == "sub"
    quota.charge(1, "v1", 2, now=NOW)
    assert store.usage_since(1, NOW - 1) == 3   # 1 + 2 (сумма weight)


def test_remaining_is_capacity_before_charge(tmp_path):
    # remaining — свободная ёмкость окна ДО списания: weight не вычитается.
    store, quota = make(tmp_path, starter=3)
    v = quota.check(1, weight=2, now=NOW)
    assert v.allowed is True
    assert v.remaining == 3


def test_renewal_extends_subscription(tmp_path):
    store, _ = make(tmp_path)
    store.activate_subscription(1, until_unix=NOW + MONTH_SEC, charge_id="c1")
    store.activate_subscription(1, until_unix=NOW + 2 * MONTH_SEC, charge_id="c2")
    assert store.subscription_until(1) == NOW + 2 * MONTH_SEC
    assert store.last_charge_id(1) == "c2"
    assert store.active_subscribers_count(now=NOW) == 1
    assert store.active_subscribers_count(now=NOW + 3 * MONTH_SEC) == 0
