"""R1: аудиофайлы в data/audio не должны копиться на диске.

Инцидент: 262 МБ сирот в data/audio, диск VPS трижды уходил в 100%.

Два независимых узла:
  (a) app/pipeline.py::_download_audio_to_chat («скачать и отправить в чат» —
      кнопка владельца) вообще не чистила скачанный файл — ни на успехе, ни
      на ошибке отправки. _process_transcription_job уже подчищала за собой
      (см. finally на _cleanup_audio_file) — тут регрессии не проверяем,
      только новый узел утечки.
  (b) app/youtube_service.py::purge_stale_audio_files — страховочная зачистка
      файлов старше 24ч в data/audio, вызывается при старте
      _transcription_queue_worker (см. app/queue_service.py).

Конвенции фейков — как в tests/test_queue_dedup.py: минимальные
классы-заглушки вместо полноценных Services/CallbackQuery.
"""
from __future__ import annotations

import time

import pytest

from app.pipeline import _download_audio_to_chat
from app.youtube_service import purge_stale_audio_files


CHAT_ID = 100
SUMMARY_MSG_ID = 42
VIDEO_ID = "dQw4w9WgXcQ"


# ── shared fakes ────────────────────────────────────────────────────────────


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeCallbackMessage:
    def __init__(self, chat_id):
        self.chat = _FakeChat(chat_id)
        self.message_id = SUMMARY_MSG_ID


class _FakeCallback:
    def __init__(self, chat_id=CHAT_ID):
        self.message = _FakeCallbackMessage(chat_id)


class _FakeProgressMessage:
    def __init__(self):
        self.edits = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class _FakeBot:
    def __init__(self, *, send_audio_error: Exception | None = None):
        self.progress_message = _FakeProgressMessage()
        self.sent_audio: list[dict] = []
        self._send_audio_error = send_audio_error

    async def send_message(self, **kwargs):
        return self.progress_message

    async def send_audio(self, **kwargs):
        if self._send_audio_error is not None:
            raise self._send_audio_error
        self.sent_audio.append(kwargs)


class _FakeYouTube:
    """download_audio возвращает заранее подготовленный на диске файл."""

    def __init__(self, audio_path):
        self._audio_path = audio_path
        self.calls = 0

    def download_audio(self, url):
        self.calls += 1
        return self._audio_path


class _FakeServices:
    def __init__(self, *, audio_path, send_audio_error=None):
        self.bot = _FakeBot(send_audio_error=send_audio_error)
        self.youtube = _FakeYouTube(audio_path)
        self.summary_cache = None  # -> fallback URL from video_id


def _make_audio_file(tmp_path, name="dQw4w9WgXcQ.mp3", size_bytes=1024):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / name
    path.write_bytes(b"0" * size_bytes)
    return path


# ── (a) успешная отправка удаляет скачанный файл ───────────────────────────


async def test_download_audio_to_chat_cleans_up_on_success(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    services = _FakeServices(audio_path=audio_path)
    callback = _FakeCallback()

    await _download_audio_to_chat(callback, VIDEO_ID, services)

    assert len(services.bot.sent_audio) == 1
    assert not audio_path.exists(), "аудиофайл должен быть удалён после успешной отправки"


# ── (b) сбой send_audio тоже удаляет скачанный файл ─────────────────────────


async def test_download_audio_to_chat_cleans_up_on_send_failure(tmp_path):
    audio_path = _make_audio_file(tmp_path)
    services = _FakeServices(audio_path=audio_path, send_audio_error=RuntimeError("boom"))
    callback = _FakeCallback()

    await _download_audio_to_chat(callback, VIDEO_ID, services)

    assert not audio_path.exists(), "аудиофайл должен быть удалён даже при сбое отправки"


# ── (c) purge_stale_audio_files: старые файлы уходят, свежие остаются ──────


def test_purge_stale_audio_files_removes_only_old_files(tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    stale = audio_dir / "stale.mp3"
    stale.write_bytes(b"old")
    fresh = audio_dir / "fresh.mp3"
    fresh.write_bytes(b"new")

    old_ts = time.time() - 25 * 3600  # 25h — старше порога 24ч
    import os
    os.utime(stale, (old_ts, old_ts))

    removed = purge_stale_audio_files(audio_dir, max_age_hours=24.0)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()


def test_purge_stale_audio_files_missing_dir_is_noop():
    removed = purge_stale_audio_files(
        __import__("pathlib").Path("/nonexistent/audio/dir/xyz"), max_age_hours=24.0
    )
    assert removed == 0
