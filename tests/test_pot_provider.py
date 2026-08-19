"""Q9: PO-token provider (bgutil-ytdlp-pot-provider) — extractor_args wiring.

Боевой инцидент 2026-08-19: YouTube требует Proof-of-Origin токены на части
роликов — web-клиент видит форматы, но медиа отдаёт HTTP 403 (репро:
I1uYg8bdBqg). Провайдер — отдельный compose-сервис, минтящий токены;
yt-dlp находит их через плагин ``youtubepot-bgutilhttp``, которому нужно
явно указать ``base_url`` провайдера (extractor_args). Эта опция должна
попасть во все места, где строятся yt-dlp options (fetch_metadata,
resolve_channel, download_audio, fetch_top_comments), и не должна ломать
fetch_transcript (он вообще без yt-dlp).

Пустой ``POT_PROVIDER_URL`` = extractor_args не добавляется — обратная
совместимость для тестов и для запуска без провайдера.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_settings
from app.db import Database
from app.utils import extract_video_id
from app.youtube_service import YouTubeService
import app.youtube_service as youtube_service_module

URL = "https://www.youtube.com/watch?v=vid1"
DEFAULT_POT_URL = "http://bgutil-provider:4416"


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    # Полная изоляция от реального .env (см. tests/test_config.py).
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")


class CapturingYDL:
    """Stand-in for yt_dlp.YoutubeDL: records the options dict it was built
    with and returns just enough info for callers not to blow up."""

    captured: list[dict] = []

    def __init__(self, options):
        self.options = options
        CapturingYDL.captured.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return {
            "title": "T",
            "channel": "C",
            "id": "vid1",
            "comments": [],
            "channel_id": "UC1",
        }


@pytest.fixture(autouse=True)
def reset_capturing_ydl():
    CapturingYDL.captured = []
    yield


def _make_service(monkeypatch, tmp_path, pot_url: str | None = DEFAULT_POT_URL) -> YouTubeService:
    if pot_url is None:
        monkeypatch.delenv("POT_PROVIDER_URL", raising=False)
    else:
        monkeypatch.setenv("POT_PROVIDER_URL", pot_url)
    settings = load_settings()
    return YouTubeService(settings, Database(tmp_path / "bot.db"))


# ---------------------------------------------------------------------------
# Settings: config default + opt-out
# ---------------------------------------------------------------------------


def test_default_pot_provider_url(base_env):
    assert load_settings().pot_provider_url == DEFAULT_POT_URL


def test_pot_provider_url_can_be_emptied(base_env, monkeypatch):
    monkeypatch.setenv("POT_PROVIDER_URL", "")
    assert load_settings().pot_provider_url == ""


# ---------------------------------------------------------------------------
# extractor_args present at every yt-dlp option-building call site
# ---------------------------------------------------------------------------


def test_fetch_metadata_adds_extractor_args(base_env, monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path)
    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", CapturingYDL)

    service.fetch_metadata(URL)

    assert CapturingYDL.captured[0]["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": [DEFAULT_POT_URL]
    }


def test_resolve_channel_adds_extractor_args(base_env, monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path)
    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", CapturingYDL)

    service.resolve_channel(URL)

    assert CapturingYDL.captured[0]["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": [DEFAULT_POT_URL]
    }


def test_fetch_top_comments_merges_with_existing_extractor_args(base_env, monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path)
    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", CapturingYDL)

    service.fetch_top_comments(URL)

    args = CapturingYDL.captured[0]["extractor_args"]
    assert args["youtubepot-bgutilhttp"] == {"base_url": [DEFAULT_POT_URL]}
    # The pre-existing comment-extractor args must survive the merge.
    assert args["youtube"]["max_comments"] == ["30"]


def test_download_audio_adds_extractor_args(base_env, monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path)
    video_id = extract_video_id(URL)

    class DownloadYDL(CapturingYDL):
        def extract_info(self, url, download=False):
            super().extract_info(url, download=download)
            tmp_dir = Path(self.options["outtmpl"]).parent
            (tmp_dir / f"{video_id}.mp3").write_bytes(b"fake-audio")
            return {}

    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", DownloadYDL)

    service.download_audio(URL)

    assert CapturingYDL.captured[0]["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": [DEFAULT_POT_URL]
    }


# ---------------------------------------------------------------------------
# Empty pot_provider_url => no extractor_args added (backward compatibility)
# ---------------------------------------------------------------------------


def test_fetch_metadata_no_extractor_args_when_unconfigured(base_env, monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, pot_url="")
    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", CapturingYDL)

    service.fetch_metadata(URL)

    assert "extractor_args" not in CapturingYDL.captured[0]


def test_resolve_channel_no_extractor_args_when_unconfigured(base_env, monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, pot_url="")
    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", CapturingYDL)

    service.resolve_channel(URL)

    assert "extractor_args" not in CapturingYDL.captured[0]


def test_fetch_top_comments_no_pot_key_when_unconfigured(base_env, monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, pot_url="")
    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", CapturingYDL)

    service.fetch_top_comments(URL)

    args = CapturingYDL.captured[0]["extractor_args"]
    assert "youtubepot-bgutilhttp" not in args
    # Existing comment-extractor args are untouched.
    assert args["youtube"]["max_comments"] == ["30"]


def test_download_audio_no_extractor_args_when_unconfigured(base_env, monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, pot_url="")
    video_id = extract_video_id(URL)

    class DownloadYDL(CapturingYDL):
        def extract_info(self, url, download=False):
            super().extract_info(url, download=download)
            tmp_dir = Path(self.options["outtmpl"]).parent
            (tmp_dir / f"{video_id}.mp3").write_bytes(b"fake-audio")
            return {}

    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", DownloadYDL)

    service.download_audio(URL)

    assert "extractor_args" not in CapturingYDL.captured[0]
