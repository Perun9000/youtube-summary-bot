from app.db import Database
from app.job_store import JobStore


def test_job_lang_persists(tmp_path):
    store = JobStore(Database(tmp_path / "bot.db"))
    job_id = store.add("https://youtu.be/x", 1, scheduled=False,
                       disable_notification=False, title_hint=None, lang="fa")
    row = store.pending()[0]
    assert row["id"] == job_id and row["lang"] == "fa"


def test_job_lang_default_ru(tmp_path):
    store = JobStore(Database(tmp_path / "bot.db"))
    store.add("https://youtu.be/x", 1, scheduled=True,
              disable_notification=True, title_hint="t")
    assert store.pending()[0]["lang"] == "ru"
