import json

from app.db import Database
from app.digest_service import DigestEntry, DigestStore
from app.monitoring_state import MonitoringState


def test_digest_add_dedup_and_limit(tmp_path):
    store = DigestStore(Database(tmp_path / "bot.db"), limit=3)
    for i in range(5):
        store.add(1, DigestEntry(video_id=f"v{i}", title=f"t{i}", telegraph_url="u", created_at_unix=i))
    entries = store.list(1)
    assert [e.video_id for e in entries] == ["v4", "v3", "v2"]  # newest-first, limit=3
    # Дедуп: повтор video_id переезжает наверх, не дублируется.
    store.add(1, DigestEntry(video_id="v3", title="new", telegraph_url="u", created_at_unix=99))
    entries = store.list(1)
    assert entries[0].video_id == "v3" and entries[0].title == "new"
    assert len(entries) == 3


def test_digest_pins(tmp_path):
    store = DigestStore(Database(tmp_path / "bot.db"))
    assert store.get_pin(1) is None
    store.set_pin(1, chat_id=10, message_id=20)
    assert store.get_pin(1) == (10, 20)
    store.clear_pin(1)
    assert store.get_pin(1) is None


def test_monitoring_state(tmp_path):
    state = MonitoringState(Database(tmp_path / "bot.db"))
    assert not state.is_seen("ch", "vid")
    state.mark_seen("ch", "vid")
    assert state.is_seen("ch", "vid")
    state.prime_channel("ch2", ["a", "b"])
    assert state.is_seen("ch2", "a") and state.is_seen("ch2", "b")


def test_monitoring_state_migrates_legacy(tmp_path):
    legacy = tmp_path / "monitoring_state.json"
    legacy.write_text(json.dumps({"channels": {"ch": {"seen_video_ids": ["x1"]}}}), encoding="utf-8")
    state = MonitoringState(Database(tmp_path / "bot.db"), legacy_json_path=legacy)
    assert state.is_seen("ch", "x1")
    assert legacy.with_suffix(".json.migrated").exists()
