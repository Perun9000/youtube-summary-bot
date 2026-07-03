import json
import time

from app.db import Database
from app.summary_cache import CachedSummary, SummaryCache
from app.user_store import UserStore


def make_cached(video_id="dQw4w9WgXcQ", created_at_unix=None):
    now = created_at_unix if created_at_unix is not None else time.time()
    return CachedSummary(
        video_id=video_id, url=f"https://youtu.be/{video_id}", title="T", channel_name="C",
        channel_url="", summary_overview="O", summary_key_points=[], summary_chapters=[],
        summary_raw_text="{}", telegraph_url="https://telegra.ph/x", transcript_url=None,
        transcript_source="youtube", model="m", created_at_iso="", created_at_unix=now,
    )


def test_user_store_roundtrip(tmp_path):
    db = Database(tmp_path / "bot.db")
    store = UserStore(db, seed_user_ids={111}, owner_user_id=42)
    assert store.is_owner(42) and store.is_allowed(111)
    store.add_user(7, "Вася")
    # Новый инстанс поверх того же файла видит те же данные.
    store2 = UserStore(Database(tmp_path / "bot.db"), seed_user_ids=set(), owner_user_id=42)
    assert any(u.user_id == 7 and u.name == "Вася" for u in store2.list_users())
    assert store2.remove_user(7) is True
    assert store2.is_allowed(7) is False


def test_user_store_migrates_legacy_json(tmp_path):
    legacy = tmp_path / "users.json"
    legacy.write_text(json.dumps({"users": [{"id": 5, "name": "старый", "added_at": "2025-01-01"}]}), encoding="utf-8")
    db = Database(tmp_path / "bot.db")
    store = UserStore(db, seed_user_ids=set(), owner_user_id=None, legacy_json_path=legacy)
    assert store.is_allowed(5)
    assert not legacy.exists() and legacy.with_suffix(".json.migrated").exists()


def test_summary_cache_roundtrip_and_ttl(tmp_path):
    db = Database(tmp_path / "bot.db")
    cache = SummaryCache(db, ttl_days=100)
    cache.put(make_cached())
    assert cache.get("dQw4w9WgXcQ").title == "T"
    assert cache.size() == 1
    # Протухшая запись удаляется лениво.
    cache.put(make_cached(video_id="expiredvid1", created_at_unix=time.time() - 101 * 86400))
    assert cache.get("expiredvid1") is None
    assert cache.delete("dQw4w9WgXcQ") is True


def test_summary_cache_migrates_legacy_json(tmp_path):
    legacy = tmp_path / "summary_cache.json"
    import dataclasses
    legacy.write_text(json.dumps({"dQw4w9WgXcQ": dataclasses.asdict(make_cached())}), encoding="utf-8")
    cache = SummaryCache(Database(tmp_path / "bot.db"), ttl_days=100, legacy_json_path=legacy)
    assert cache.get("dQw4w9WgXcQ") is not None
    assert legacy.with_suffix(".json.migrated").exists()
