import threading
import time

from app.db import Database
from app.youtube_service import YtdlpUsage


def test_counts_and_rollover(tmp_path, monkeypatch):
    db = Database(tmp_path / "bot.db")
    usage = YtdlpUsage(db, min_interval_sec=0, soft_daily_limit=100)
    fake_day = {"value": "2026-07-06"}
    monkeypatch.setattr(usage, "_today", lambda: fake_day["value"])
    usage.before_call()
    usage.before_call()
    assert usage.today_count() == 2
    fake_day["value"] = "2026-07-07"          # новый день — счётчик с нуля
    usage.before_call()
    assert usage.today_count() == 1


def test_min_interval_enforced(tmp_path):
    usage = YtdlpUsage(Database(tmp_path / "bot.db"), min_interval_sec=0.2, soft_daily_limit=100)
    started = time.monotonic()
    usage.before_call()
    usage.before_call()   # должен подождать ~0.2 сек после первого
    assert time.monotonic() - started >= 0.2


def test_none_db_is_noop():
    usage = YtdlpUsage(None, min_interval_sec=0, soft_daily_limit=100)
    usage.before_call()
    assert usage.today_count() == 0


def test_concurrent_calls_do_not_lose_counts(tmp_path, monkeypatch):
    """Два параллельных to_thread-потока не должны терять инкременты.

    SELECT → INSERT OR REPLACE без общего lock'а — классический lost update:
    оба потока читают одинаковый count, второй перезаписывает первого.
    """
    usage = YtdlpUsage(Database(tmp_path / "bot.db"), min_interval_sec=0, soft_daily_limit=1000)
    monkeypatch.setattr(usage, "_today", lambda: "2026-07-06")

    def worker() -> None:
        for _ in range(20):
            usage.before_call()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert usage.today_count() == 40
