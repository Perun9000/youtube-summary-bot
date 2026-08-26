"""Q13: сетевые ретраи для транскрипционного пути.

Инцидент 2026-08-26: разовый Read timeout на скачивании аудио (googlevideo)
приводил к мгновенному failed с ошибкой пользователю, хотя через минуту
скачивание снова шло на полной скорости. `_process_transcription_job` был
единственным конвейером без Q4-авторетрая (см. app/pipeline.py::
_maybe_retry_transient_failure) — эта задача переиспользует ровно тот же
деферрал-путь в error-воронке транскрипции.

Конвенции фейков — как в tests/test_transient_retry.py.
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

from app.db import Database
from app.job_store import JobStore
from app.pipeline import MAX_TRANSIENT_RETRIES, _process_transcription_job
from app.services_container import SummaryJob


CHAT_ID = 100
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class _FakeYouTube:
    """download_audio либо бросает заданное исключение, либо возвращает путь."""

    def __init__(self, exc: Exception | None = None, path: str = "/tmp/fake.mp3"):
        self._exc = exc
        self._path = path
        self.calls = 0

    def download_audio(self, url):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._path


class _FakeGroqWhisper:
    async def transcribe(self, path):
        return []


class _FakeSentMessage:
    def __init__(self):
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class _FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return _FakeSentMessage()


class _FakeServices:
    def __init__(self, youtube: _FakeYouTube, job_store: JobStore):
        self.youtube = youtube
        self.groq_whisper = _FakeGroqWhisper()
        self.job_store = job_store
        self.bot = _FakeBot()
        self.pending_custom_prompts = {}

        self.summary_queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
        self.summary_queue_lock = asyncio.Lock()
        self.summary_active_job = None
        self.summary_worker_task = None
        self.summary_next_sequence = 0
        self.summary_status_messages = {}
        self.summary_status_base_texts = {}
        self.summary_status_parse_modes = {}
        self.summary_status_disable_previews = {}


def _make_job_store(tmp_path) -> JobStore:
    return JobStore(Database(tmp_path / "bot.db"))


def _make_job(
    job_store: JobStore, *, transient_retries: int = 0, chat_id: int = CHAT_ID
) -> SummaryJob:
    db_id = job_store.add(
        URL, chat_id, scheduled=False, disable_notification=False, title_hint=None, lang="ru",
    )
    job_store.set_status(db_id, "active")
    return SummaryJob(
        sequence=1,
        message=None,
        url=URL,
        enqueued_at=time.monotonic(),
        chat_id=chat_id,
        db_id=db_id,
        lang="ru",
        transient_retries=transient_retries,
    )


# ── (a) транзиентная ошибка при скачивании аудио → deferred, НЕ failed ────


async def test_transient_download_failure_defers_job_instead_of_failing(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = aiohttp.ClientConnectionError("network storm")
    services = _FakeServices(_FakeYouTube(exc=exc), job_store)
    job = _make_job(job_store, transient_retries=0)
    before = time.time()

    await _process_transcription_job(job, services)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 1
    assert before + 295 <= row["run_after"] <= time.time() + 305

    # Никакого финального failure-сообщения — только статусы (audio_download +
    # retry_scheduled), оба эдитами одного service-status сообщения.
    assert len(services.bot.sent) == 1
    sent_message = services.bot.sent[0]
    assert "error" not in sent_message["text"].lower()

    status_message = services.summary_status_messages[CHAT_ID]
    assert "Сетевая заминка — повторю через 5 мин." in status_message.edits[-1]

    # Job не должен уйти дальше в summary_queue — он отложен, а не переигран
    # синхронно.
    assert services.summary_queue.empty()


# ── (b) лимит попыток исчерпан → прежнее поведение: failed + сообщение ────


async def test_third_transient_download_failure_falls_back_to_failed(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = aiohttp.ClientConnectionError("network storm")
    services = _FakeServices(_FakeYouTube(exc=exc), job_store)
    assert MAX_TRANSIENT_RETRIES == 3
    job = _make_job(job_store, transient_retries=MAX_TRANSIENT_RETRIES)

    await _process_transcription_job(job, services)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "failed"

    # Финальное сообщение об ошибке ушло вторым send_message-вызовом (первый —
    # статус "скачиваю аудио").
    assert len(services.bot.sent) == 2
    error_text = services.bot.sent[-1]["text"]
    assert error_text


# ── (c) нетранзиентная ошибка → failed сразу, счётчик попыток не растёт ───


async def test_non_transient_download_failure_fails_immediately(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = RuntimeError("Video unavailable: Private video")
    services = _FakeServices(_FakeYouTube(exc=exc), job_store)
    job = _make_job(job_store, transient_retries=0)

    await _process_transcription_job(job, services)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "failed"
    assert row["attempts"] == 0

    assert len(services.bot.sent) == 2
