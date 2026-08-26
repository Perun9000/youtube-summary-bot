"""R4: утечка tmp-директорий yt-XXXXXX в data/audio.

download_audio скачивает через ``tempfile.mkdtemp(prefix="yt-", dir=audio_dir)``
и переносит готовый mp3 наружу через ``.replace()`` — саму tmp-директорию
никто не удалял: ни на успехе (остаётся пустой), ни на исключении (остаётся
с недокачанным мусором yt-dlp внутри). См. R1 (262 МБ сирот, VPS диск на
100% трижды) — это тот же класс утечки, только на уровень директорий, а не
файлов.

Конвенции — как в tests/test_pot_provider.py: ``CapturingYDL`` подменяет
``yt_dlp.YoutubeDL``, ``load_settings()`` + ``base_env`` фикстура для
изоляции от реального ``.env``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_settings
from app.db import Database
from app.utils import extract_video_id
from app.youtube_service import YouTubeService, purge_stale_audio_files
import app.youtube_service as youtube_service_module

URL = "https://www.youtube.com/watch?v=vid1"


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    monkeypatch.delenv("POT_PROVIDER_URL", raising=False)


def _make_service(tmp_path) -> YouTubeService:
    settings = load_settings()
    return YouTubeService(settings, Database(tmp_path / "bot.db"))


class CapturingYDL:
    """Records the options dict it was built with (outtmpl -> tmp_dir)."""

    captured: list[dict] = []

    def __init__(self, options):
        self.options = options
        CapturingYDL.captured.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def reset_capturing_ydl():
    CapturingYDL.captured = []
    yield


def _tmp_dir_from_options(options: dict) -> Path:
    return Path(options["outtmpl"]).parent


# ── (a) успешное скачивание удаляет tmp-директорию ─────────────────────────


def test_download_audio_removes_tmp_dir_after_success(base_env, monkeypatch, tmp_path):
    service = _make_service(tmp_path)
    video_id = extract_video_id(URL)

    class DownloadYDL(CapturingYDL):
        def extract_info(self, url, download=False):
            tmp_dir = _tmp_dir_from_options(self.options)
            (tmp_dir / f"{video_id}.mp3").write_bytes(b"fake-audio")
            return {}

    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", DownloadYDL)

    result = service.download_audio(URL)

    assert result.exists()  # финальный mp3 остался (переехал в audio_dir напрямую)
    tmp_dir = _tmp_dir_from_options(CapturingYDL.captured[0])
    assert not tmp_dir.exists(), "tmp-директория yt-XXXXXX должна быть удалена после успеха"


# ── (b) сбой скачивания тоже удаляет tmp-директорию ─────────────────────────


def test_download_audio_removes_tmp_dir_after_exception(base_env, monkeypatch, tmp_path):
    service = _make_service(tmp_path)

    class ExplodingYDL(CapturingYDL):
        def extract_info(self, url, download=False):
            # Симулируем недокачанный мусор (.part) перед сетевым сбоем —
            # rmtree должен снести его вместе с директорией.
            tmp_dir = _tmp_dir_from_options(self.options)
            (tmp_dir / "leftover.part").write_bytes(b"partial")
            raise RuntimeError("Read timed out")

    monkeypatch.setattr(youtube_service_module.yt_dlp, "YoutubeDL", ExplodingYDL)

    with pytest.raises(RuntimeError):
        service.download_audio(URL)

    tmp_dir = _tmp_dir_from_options(CapturingYDL.captured[0])
    assert not tmp_dir.exists(), "tmp-директория должна быть удалена даже при сбое скачивания"


# ── (c) purge_stale_audio_files: пустая старая поддиректория удаляется ─────


def test_purge_stale_audio_files_removes_stale_empty_subdir(tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    stale_dir = audio_dir / "yt-stale123"
    stale_dir.mkdir()
    fresh_dir = audio_dir / "yt-fresh456"
    fresh_dir.mkdir()

    import os
    import time

    old_ts = time.time() - 25 * 3600  # 25h — старше порога 24ч
    os.utime(stale_dir, (old_ts, old_ts))

    purge_stale_audio_files(audio_dir, max_age_hours=24.0)

    assert not stale_dir.exists(), "старая пустая поддиректория должна быть удалена"
    assert fresh_dir.exists(), "свежую поддиректорию трогать нельзя"


def test_purge_stale_audio_files_does_not_remove_nonempty_stale_subdir(tmp_path):
    """Непустая (например, ещё активная скачка) директория не сносится
    целиком — только её просроченные файлы, на следующем проходе."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    busy_dir = audio_dir / "yt-busy789"
    busy_dir.mkdir()
    fresh_file = busy_dir / "in_progress.part"
    fresh_file.write_bytes(b"still downloading")

    import os
    import time

    old_ts = time.time() - 25 * 3600
    os.utime(busy_dir, (old_ts, old_ts))

    purge_stale_audio_files(audio_dir, max_age_hours=24.0)

    assert busy_dir.exists(), "непустая директория не должна сноситься целиком"
    assert fresh_file.exists()
