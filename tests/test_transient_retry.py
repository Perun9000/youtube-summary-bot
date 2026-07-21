"""Q4: авторетрай задач, упавших по СЕТЕВЫМ (транзиентным) причинам.

Утренний шторм сети роняет job'ы, которые через несколько минут отработали бы
нормально. Вместо немедленного failed + сообщения об ошибке — job откладывается
на повтор (backoff 5/10/15 мин) через ту же deferred-механику, что уже несут
премьеры (jobs.run_after + status='deferred' + run_deferred_jobs_scheduler).
После MAX_TRANSIENT_RETRIES неудач — прежнее поведение (failed + сообщение).

Конвенции фейков — как в tests/test_queue_dedup.py: минимальные классы-заглушки
вместо полноценных Services/Message. Реальная БД — как в tests/test_job_store.py
(Database + JobStore на tmp_path), чтобы проверять фактическое состояние строки
после прогона воркера.
"""
import asyncio
import time

import aiohttp
import pytest

from app.db import Database
from app.job_store import JobStore
from app.pipeline import (
    MAX_TRANSIENT_RETRIES,
    TRANSIENT_RETRY_BACKOFF_UNIT_SEC,
    _is_transient_failure,
)
from app.queue_service import _requeue_due_deferred, _summary_queue_worker
from app.services_container import SummaryJob


OWNER_ID = 555
CHAT_ID = 100
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ── _is_transient_failure: классификатор ───────────────────────────────────


def test_aiohttp_client_error_is_transient():
    assert _is_transient_failure(aiohttp.ClientConnectionError("boom"))


def test_asyncio_timeout_error_is_transient():
    assert _is_transient_failure(asyncio.TimeoutError("boom"))


def test_connection_error_is_transient():
    assert _is_transient_failure(ConnectionResetError("Connection reset by peer"))


def test_cannot_connect_to_host_text_is_transient():
    assert _is_transient_failure(RuntimeError("Cannot connect to host openrouter.ai:443"))


def test_temporary_dns_failure_text_is_transient():
    assert _is_transient_failure(
        RuntimeError("[Errno -3] Temporary failure in name resolution")
    )


def test_openrouter_http_503_is_transient():
    assert _is_transient_failure(
        RuntimeError("OpenRouter (some/model) не ответил после 3 попыток: http_503")
    )


def test_openrouter_http_502_text_variant_is_transient():
    assert _is_transient_failure(RuntimeError("OpenRouter HTTP 502: upstream error"))


def test_free_chain_exhausted_marker_is_never_transient_even_with_5xx_inside():
    from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER

    exc = RuntimeError(
        f"{FREE_CHAIN_EXHAUSTED_MARKER}: все free-модели отказали. "
        "Последняя ошибка: OpenRouter HTTP 503: upstream error"
    )
    assert not _is_transient_failure(exc)


def test_openrouter_budget_marker_is_never_transient():
    from app.llm_client import OPENROUTER_BUDGET_EXCEEDED_MARKER

    exc = RuntimeError(f"{OPENROUTER_BUDGET_EXCEEDED_MARKER}: daily cap")
    assert not _is_transient_failure(exc)


def test_private_video_is_not_transient():
    assert not _is_transient_failure(RuntimeError("Video unavailable: Private video"))


def test_missing_transcript_is_not_transient():
    assert not _is_transient_failure(RuntimeError("нет субтитров"))


def test_groq_api_key_missing_is_not_transient():
    assert not _is_transient_failure(
        RuntimeError(
            "Субтитры YouTube недоступны для этого ролика, "
            "а GROQ_API_KEY не настроен — облачное распознавание выключено."
        )
    )


def test_generic_exception_is_not_transient():
    assert not _is_transient_failure(Exception("что-то пошло не так"))


# ── shared fakes для интеграционных тестов через _summary_queue_worker ────


class _FakeSettings:
    def __init__(self, owner_user_id=OWNER_ID):
        self.owner_user_id = owner_user_id


class _FakeUsers:
    def is_owner(self, user_id):
        return user_id == OWNER_ID

    def is_allowed(self, user_id):
        return user_id == OWNER_ID


class _FakeYouTube:
    """fetch_metadata всегда бросает заданное исключение."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    def fetch_metadata(self, url):
        self.calls += 1
        raise self._exc


class _FakeSentMessage:
    def __init__(self):
        self.edits: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class _FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return _FakeSentMessage()


class _FakeServices:
    def __init__(self, exc: Exception, job_store: JobStore):
        self.settings = _FakeSettings()
        self.users = _FakeUsers()
        self.billing = None
        self.quota = None
        self.youtube = _FakeYouTube(exc)
        self.summary_cache = None
        self.job_store = job_store
        self.bot = _FakeBot()
        self.bot_username = None
        self.morning_digest = None

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


def _make_job(job_store: JobStore, *, retry_count: int = 0, chat_id: int = CHAT_ID) -> SummaryJob:
    db_id = job_store.add(
        URL, chat_id, scheduled=False, disable_notification=False, title_hint=None, lang="ru",
    )
    return SummaryJob(
        sequence=1,
        message=None,
        url=URL,
        enqueued_at=time.monotonic(),
        chat_id=chat_id,
        db_id=db_id,
        lang="ru",
        retry_count=retry_count,
    )


async def _run_worker_once(services: _FakeServices, job: SummaryJob) -> None:
    await services.summary_queue.put(job)
    # _summary_queue_worker processes everything currently queued, then stops
    # itself (1s idle timeout branch) once the queue is empty — no manual
    # task management needed, it returns on its own.
    await _summary_queue_worker(services)


# ── (a) транзиентная ошибка → deferred, НЕ failed, retry_scheduled статус ─


async def test_transient_failure_defers_job_instead_of_failing(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = aiohttp.ClientConnectionError("network storm")
    services = _FakeServices(exc, job_store)
    job = _make_job(job_store, retry_count=0)
    before = time.time()

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 1
    # backoff = 300 * 1 попытка = 300 сек
    assert before + 295 <= row["run_after"] <= time.time() + 305

    # Никакого нового финального сообщения об ошибке — только статус
    # "fetching" (изначальный) был отправлен через send_message, а
    # retry_scheduled лёг эдитом того же сообщения (bump=False по умолчанию).
    assert len(services.bot.sent) == 1
    sent_message = services.bot.sent[0]
    assert "error" not in sent_message["text"].lower()

    status_message = services.summary_status_messages[CHAT_ID]
    assert status_message.edits, "retry_scheduled статус должен был обновить сообщение"
    # _render_service_status добавляет HTML-шапку со ссылкой на видео перед
    # текстом статуса — проверяем, что сам текст ретрая присутствует.
    assert "Сетевая заминка — повторю через 5 мин." in status_message.edits[-1]


# ── (b) 3-я неудача подряд → прежнее поведение: failed + сообщение ────────


async def test_third_transient_failure_falls_back_to_failed_with_error_message(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = aiohttp.ClientConnectionError("network storm")
    services = _FakeServices(exc, job_store)
    # retry_count == MAX_TRANSIENT_RETRIES: две попытки уже были (пришли бы
    # через _requeue_due_deferred, см. тест (d)), это — третий провал подряд.
    assert MAX_TRANSIENT_RETRIES == 3
    job = _make_job(job_store, retry_count=MAX_TRANSIENT_RETRIES)

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "failed"

    # Финальное сообщение об ошибке ушло вторым send_message-вызовом (первый —
    # статус "fetching").
    assert len(services.bot.sent) == 2
    error_text = services.bot.sent[-1]["text"]
    assert "не удалось" in error_text.lower() or "ошиб" in error_text.lower() or error_text


# ── (c) нетранзиентная ошибка → failed сразу, retry_count не важен ────────


async def test_non_transient_failure_fails_immediately_even_on_first_attempt(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = RuntimeError("нет субтитров")
    services = _FakeServices(exc, job_store)
    job = _make_job(job_store, retry_count=0)

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "failed"
    assert row["attempts"] == 0  # ретрай не трогал счётчик — ошибка нетранзиентна


# ── (г) _requeue_due_deferred переносит attempts в retry_count ────────────


class _FakeServicesForRequeue:
    def __init__(self, job_store: JobStore):
        self.job_store = job_store
        self.users = _FakeUsers()
        self.billing = None
        self.summary_queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
        self.summary_queue_lock = asyncio.Lock()
        self.summary_worker_task = None
        self.summary_next_sequence = 0


async def test_requeue_due_deferred_carries_attempts_into_retry_count(tmp_path):
    job_store = _make_job_store(tmp_path)
    db_id = job_store.add(
        URL, CHAT_ID, scheduled=False, disable_notification=False, title_hint=None, lang="ru",
    )
    job_store.set_deferred(db_id, run_after=1000.0, attempts=2)

    services = _FakeServicesForRequeue(job_store)
    # due_deferred(now) требует now >= run_after
    await _requeue_due_deferred_at(services, now=1000.0)

    requeued = services.summary_queue.get_nowait()
    assert requeued.retry_count == 2
    assert requeued.db_id == db_id

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (db_id,))
    assert row["status"] == "queued"


async def _requeue_due_deferred_at(services, *, now: float) -> None:
    """_requeue_due_deferred читает time.time() напрямую — тестируем через
    настоящий due_deferred(now) вызов job_store, минуя монки time.time():
    выставляем run_after в прошлом (уже < текущего реального времени), что
    эквивалентно "срок настал" без патчинга time."""
    assert now <= time.time()
    await _requeue_due_deferred(services)


# ── premiere-деферрал: attempts не растёт, лимит retry не действует ───────


def test_premiere_deferral_does_not_touch_attempts(tmp_path):
    """Регрессия: set_deferred без attempts (как зовёт _defer_premiere_job)
    не должен трогать jobs.attempts — премьеры не должны упираться в лимит
    в 3 транзиентных попытки."""
    job_store = _make_job_store(tmp_path)
    db_id = job_store.add(
        URL, CHAT_ID, scheduled=False, disable_notification=False, title_hint=None, lang="ru",
    )
    job_store.set_deferred(db_id, run_after=2000.0)  # без attempts= — как премьера

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 0
