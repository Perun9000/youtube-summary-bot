from app.analytics_events import AnalyticsEvents
from app.billing import BillingStore
from app.db import Database


NOW = 1_000_000.0
DAY = 86_400.0


def test_first_start_is_idempotent(tmp_path):
    a = AnalyticsEvents(Database(tmp_path / "bot.db"))
    assert a.record_first_start(1, "organic", now=NOW) is True
    assert a.record_first_start(1, "deep_link", now=NOW + 10) is False
    f = a.funnel(days=30, now=NOW + 20)
    assert f["first_starts"] == 1


def test_funnel_counts(tmp_path):
    db = Database(tmp_path / "bot.db")
    a = AnalyticsEvents(db)
    billing = BillingStore(db)

    # Три новых пользователя пришли в окне.
    for uid, src in ((1, "organic"), (2, "deep_link"), (3, "organic")):
        a.record_first_start(uid, src, now=NOW)
    # Двое дошли до первой генерации (usage_events)...
    billing.record_usage(1, "vid1", 1, "starter", now=NOW + 100)
    billing.record_usage(2, "vid2", 1, "starter", now=NOW + 200)
    # ...у первого это уже вторая генерация — первой она быть не перестаёт.
    billing.record_usage(1, "vid3", 1, "starter", now=NOW + 300)
    # Один упёрся в лимит (дважды — считаем уникальных).
    a.record(1, "quota_denied", "weekly_exhausted", now=NOW + 400)
    a.record(1, "quota_denied", "weekly_exhausted", now=NOW + 500)
    # И подписался; потом одно продление.
    a.record(1, "sub_activated", "ch_1", now=NOW + 600)
    a.record(1, "sub_renewed", "ch_2", now=NOW + 700)

    f = a.funnel(days=30, now=NOW + 1000)
    assert f == {
        "first_starts": 3,
        "first_generations": 2,
        "quota_denied_users": 1,
        "subs_activated": 1,
        "sub_renewals": 1,
    }


def test_funnel_window_excludes_old(tmp_path):
    db = Database(tmp_path / "bot.db")
    a = AnalyticsEvents(db)
    billing = BillingStore(db)
    a.record_first_start(1, "organic", now=NOW - 40 * DAY)     # старый юзер
    billing.record_usage(1, "old", 1, "starter", now=NOW - 40 * DAY)
    billing.record_usage(1, "new", 1, "free", now=NOW)          # НЕ первая генерация
    f = a.funnel(days=30, now=NOW + 1)
    assert f["first_starts"] == 0
    assert f["first_generations"] == 0


def test_record_never_raises_on_bad_db(tmp_path):
    db = Database(tmp_path / "bot.db")
    a = AnalyticsEvents(db)
    db.close()
    # После закрытия БД запись не должна кидать — только warning в лог.
    a.record(1, "quota_denied", "weekly_exhausted", now=NOW)
    assert a.record_first_start(2, "organic", now=NOW) is False
