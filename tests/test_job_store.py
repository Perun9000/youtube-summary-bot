from app.db import Database
from app.job_store import JobStore


def test_job_lifecycle(tmp_path):
    store = JobStore(Database(tmp_path / "bot.db"))
    job_id = store.add("https://youtu.be/x", 42, scheduled=False, disable_notification=False, title_hint=None)
    assert [r["id"] for r in store.pending()] == [job_id]
    store.set_status(job_id, "active")
    assert [r["status"] for r in store.pending()] == ["active"]
    store.set_status(job_id, "done")
    assert store.pending() == []
    assert store.counts_since(30)["done"] == 1


def test_scheduled_pending_count(tmp_path):
    store = JobStore(Database(tmp_path / "bot.db"))
    store.add("u1", 1, scheduled=True, disable_notification=True, title_hint="t")
    j2 = store.add("u2", 1, scheduled=False, disable_notification=False, title_hint=None)
    assert store.scheduled_pending_count() == 1
    store.set_status(j2, "done")
    assert store.scheduled_pending_count() == 1


def test_deferred_lifecycle(tmp_path):
    store = JobStore(Database(tmp_path / "bot.db"))
    job_id = store.add("https://youtu.be/x", 42, scheduled=False, disable_notification=False, title_hint=None)
    store.set_deferred(job_id, run_after=1000.0)
    assert store.pending() == []                    # deferred — не queued/active
    assert store.due_deferred(999.0) == []          # время ещё не пришло
    assert [r["id"] for r in store.due_deferred(1000.0)] == [job_id]
    store.set_status(job_id, "queued")              # deferred-scheduler поднял
    assert [r["id"] for r in store.pending()] == [job_id]
    assert store.due_deferred(2000.0) == []
