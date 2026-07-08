from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_settings
from app.db import Database
from app.youtube_service import CookieRotator, YouTubeService, YtdlpUsage
import app.youtube_service as youtube_service_module


BAN_MESSAGE = (
    "ERROR: [youtube] vid1: Sign in to confirm you're not a bot. "
    "This helps protect our community."
)
URL = "https://www.youtube.com/watch?v=vid1"


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    # Полная изоляция от реального .env (см. tests/test_config.py).
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")


def _write_accounts(cookies_dir: Path, names: list[str]) -> None:
    cookies_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (cookies_dir / f"{name}.txt").write_text("# Netscape HTTP Cookie File\n")


class FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL: records which cookiefile was used and
    raises a ban-style error for accounts listed in ``fail_accounts``."""

    calls: list[str | None] = []
    fail_accounts: set[str] = set()

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        cookiefile = self.options.get("cookiefile")
        account = Path(cookiefile).stem if cookiefile else None
        FakeYDL.calls.append(account)
        if account in FakeYDL.fail_accounts:
            raise Exception(BAN_MESSAGE)
        return {"title": f"Video via {account}", "channel": "Channel", "id": "vid1"}


@pytest.fixture(autouse=True)
def reset_fake_ydl():
    FakeYDL.calls = []
    FakeYDL.fail_accounts = set()
    yield


# ---------------------------------------------------------------------------
# CookieRotator: round-robin, cooldown, least-recently-failed fallback
# ---------------------------------------------------------------------------


def test_round_robin_over_three_accounts(tmp_path):
    cookies_dir = tmp_path / "cookies"
    _write_accounts(cookies_dir, ["acc1", "acc2", "acc3"])
    rotator = CookieRotator(cookies_dir)

    firsts = [next(rotator.iter_pool()).name for _ in range(6)]

    assert firsts == ["acc1", "acc2", "acc3", "acc1", "acc2", "acc3"]


def test_empty_dir_disables_rotation(tmp_path):
    rotator = CookieRotator(tmp_path / "does-not-exist")
    assert rotator.enabled is False
    assert list(rotator.iter_pool()) == []


def test_cooldown_moves_account_to_back_of_pool(tmp_path):
    cookies_dir = tmp_path / "cookies"
    _write_accounts(cookies_dir, ["acc1", "acc2", "acc3"])
    rotator = CookieRotator(cookies_dir)

    rotator.mark_cooldown("acc1", reason="Sign in to confirm")
    order = [a.name for a in rotator.iter_pool()]

    # Round robin still starts at acc1 (cursor advanced regardless), but the
    # cooling-down account is pushed after the live ones within that lap.
    assert order[0] != "acc1"
    assert order[-1] == "acc1"
    assert set(order) == {"acc1", "acc2", "acc3"}

    status = {entry["name"]: entry for entry in rotator.status()}
    assert status["acc1"]["cooldown"] is True
    assert status["acc2"]["cooldown"] is False


def test_all_cooling_down_falls_back_to_least_recently_failed(tmp_path, monkeypatch):
    cookies_dir = tmp_path / "cookies"
    _write_accounts(cookies_dir, ["acc1", "acc2"])
    rotator = CookieRotator(cookies_dir)

    times = iter([100.0, 200.0])
    monkeypatch.setattr(youtube_service_module.time, "monotonic", lambda: next(times))
    rotator.mark_cooldown("acc1", reason="429")  # cooldown_until ~100+6h
    rotator.mark_cooldown("acc2", reason="429")  # cooldown_until ~200+6h, more recent failure

    # "now" for iter_pool's live-check still returns from the same iterator;
    # patch a fixed "now" far before either cooldown expires.
    monkeypatch.setattr(youtube_service_module.time, "monotonic", lambda: 150.0)
    order = [a.name for a in rotator.iter_pool()]

    # Both cooling down -> least-recently-failed (acc1, failed at 100) first.
    assert order[0] == "acc1"


# ---------------------------------------------------------------------------
# YtdlpUsage: per-account counters, backward-compatible global counter
# ---------------------------------------------------------------------------


def test_per_account_counters_and_backward_compatible_global(tmp_path):
    db = Database(tmp_path / "bot.db")
    usage = YtdlpUsage(db, min_interval_sec=0, soft_daily_limit=1000)

    usage.before_call("acc1")
    usage.before_call("acc1")
    usage.before_call("acc2")
    usage.before_call()  # legacy call with no account, still bumps the global counter

    assert usage.today_count() == 4
    assert usage.account_counts_today() == {"acc1": 2, "acc2": 1}


def test_account_counts_empty_when_no_accounts_used(tmp_path):
    db = Database(tmp_path / "bot.db")
    usage = YtdlpUsage(db, min_interval_sec=0, soft_daily_limit=1000)
    usage.before_call()
    assert usage.account_counts_today() == {}


# ---------------------------------------------------------------------------
# YouTubeService integration: rotation + cooldown + retry, and legacy fallback
# ---------------------------------------------------------------------------


def test_single_cookie_mode_unchanged_when_cookies_dir_empty(base_env, monkeypatch, tmp_path):
    cookies_path = tmp_path / "youtube.cookies.txt"
    cookies_path.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("YTDLP_COOKIES_PATH", str(cookies_path))
    # YTDLP_COOKIES_DIR left at its default (BOT_DATA_DIR/cookies) which
    # doesn't exist here => rotation must stay disabled.
    settings = load_settings()
    service = YouTubeService(settings, Database(tmp_path / "bot.db"))
    assert service.cookie_rotation_enabled() is False

    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", FakeYDL)
    metadata = service.fetch_metadata(URL)

    assert metadata.title == "Video via youtube.cookies"
    assert FakeYDL.calls == ["youtube.cookies"]


def test_rotation_switches_account_on_ban_error(base_env, monkeypatch, tmp_path):
    monkeypatch.setenv("YTDLP_COOKIES_DIR", str(tmp_path / "cookies"))
    _write_accounts(tmp_path / "cookies", ["acc1", "acc2", "acc3"])
    settings = load_settings()
    service = YouTubeService(settings, Database(tmp_path / "bot.db"))
    assert service.cookie_rotation_enabled() is True

    FakeYDL.fail_accounts = {"acc1"}
    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", FakeYDL)

    metadata = service.fetch_metadata(URL)

    assert metadata.title == "Video via acc2"
    assert FakeYDL.calls == ["acc1", "acc2"]

    status = {entry["name"]: entry for entry in service.cookie_account_status()}
    assert status["acc1"]["cooldown"] is True
    assert status["acc2"]["cooldown"] is False
    assert status["acc2"]["count_today"] == 1


def test_rotation_gives_up_after_one_lap_when_all_accounts_banned(base_env, monkeypatch, tmp_path):
    monkeypatch.setenv("YTDLP_COOKIES_DIR", str(tmp_path / "cookies"))
    _write_accounts(tmp_path / "cookies", ["acc1", "acc2"])
    settings = load_settings()
    service = YouTubeService(settings, Database(tmp_path / "bot.db"))

    FakeYDL.fail_accounts = {"acc1", "acc2"}
    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", FakeYDL)

    # fetch_metadata swallows the failure into a fallback VideoMetadata
    # (unchanged behavior), but must have tried exactly the pool size.
    metadata = service.fetch_metadata(URL)

    assert metadata.title == "YouTube video vid1"
    assert FakeYDL.calls == ["acc1", "acc2"]


def test_non_ban_error_does_not_rotate(base_env, monkeypatch, tmp_path):
    monkeypatch.setenv("YTDLP_COOKIES_DIR", str(tmp_path / "cookies"))
    _write_accounts(tmp_path / "cookies", ["acc1", "acc2"])
    settings = load_settings()
    service = YouTubeService(settings, Database(tmp_path / "bot.db"))

    class BoomYDL(FakeYDL):
        def extract_info(self, url, download=False):
            FakeYDL.calls.append("acc1-attempt")
            raise Exception("some unrelated network error")

    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", BoomYDL)

    metadata = service.fetch_metadata(URL)  # fetch_metadata catches everything -> fallback

    assert metadata.title == "YouTube video vid1"
    assert FakeYDL.calls == ["acc1-attempt"]  # only one attempt, no rotation
