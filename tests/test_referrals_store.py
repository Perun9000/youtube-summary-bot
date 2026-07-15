from app.db import Database
from app.referrals_store import ReferralsStore


def _store(tmp_path):
    return ReferralsStore(Database(tmp_path / "bot.db"))


def test_bind_first_touch_wins(tmp_path):
    store = _store(tmp_path)
    assert store.bind(user_id=10, referrer_id=1, video_id="abcABC12345") is True
    assert store.bind(user_id=10, referrer_id=2, video_id="otherVID123") is False
    assert store.referrer_of(10) == 1


def test_bind_rejects_self_referral(tmp_path):
    store = _store(tmp_path)
    assert store.bind(user_id=5, referrer_id=5) is False
    assert store.referrer_of(5) is None


def test_referrer_of_unknown_user(tmp_path):
    assert _store(tmp_path).referrer_of(404) is None
