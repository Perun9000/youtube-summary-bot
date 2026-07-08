import time

from app.db import Database
from app.summary_cache import CachedSummary, SummaryCache


def make_cached(vid="dQw4w9WgXcQ", overview="O"):
    return CachedSummary(
        video_id=vid, url=f"https://youtu.be/{vid}", title="T", channel_name="C",
        channel_url="", summary_overview=overview, summary_key_points=[],
        summary_chapters=[], summary_raw_text="{}", telegraph_url="https://telegra.ph/x",
        transcript_url=None, transcript_source="youtube", model="m",
        created_at_iso="", created_at_unix=time.time(),
    )


def test_cache_is_per_language(tmp_path):
    cache = SummaryCache(Database(tmp_path / "bot.db"))
    cache.put(make_cached(overview="русское"), lang="ru")
    cache.put(make_cached(overview="english"), lang="en")
    assert cache.get("dQw4w9WgXcQ", lang="ru").summary_overview == "русское"
    assert cache.get("dQw4w9WgXcQ", lang="en").summary_overview == "english"
    assert cache.get("dQw4w9WgXcQ", lang="fa") is None


def test_legacy_ru_rows_still_found(tmp_path):
    db = Database(tmp_path / "bot.db")
    cache = SummaryCache(db)
    cache.put(make_cached(), lang="ru")   # ru живёт под голым video_id
    row = db.query_one("SELECT video_id FROM summary_cache")
    assert row["video_id"] == "dQw4w9WgXcQ"


def test_delete_drops_all_languages(tmp_path):
    cache = SummaryCache(Database(tmp_path / "bot.db"))
    cache.put(make_cached(), lang="ru")
    cache.put(make_cached(), lang="en")
    cache.put(make_cached(vid="othervideo1"), lang="en")
    assert cache.delete("dQw4w9WgXcQ") is True
    assert cache.get("dQw4w9WgXcQ", lang="ru") is None
    assert cache.get("dQw4w9WgXcQ", lang="en") is None
    assert cache.get("othervideo1", lang="en") is not None
