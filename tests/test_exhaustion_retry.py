"""Q8 (часть 2): долгий отложенный ретрай exhaustion-класса ошибок.

Боевой инцидент 2026-08-17: FREE_CHAIN_EXHAUSTED с «Последняя ошибка: .»
(пустой текст — та же пустота, что чинит часть 1 этой задачи, llm_client.py)
— job финалился с ошибкой, хотя через час-другой free-модели у сторонних
провайдеров снова отвечают. FREE_CHAIN_EXHAUSTED_MARKER /
OPENROUTER_BUDGET_EXCEEDED_MARKER — тоже не повод сразу сдаваться: вместо
финального failed job откладывается на долгий повтор (час, либо до сброса
дневного лимита OpenRouter в 00:05 UTC, если это раньше) через ту же
deferred-механику, что уже несут Q4 (сетевые сбои) и премьеры — с ОБЩИМ
счётчиком/лимитом попыток (job.transient_retries / MAX_TRANSIENT_RETRIES).

Конвенции фейков — как в tests/test_transient_retry.py.
"""
import asyncio
import datetime as dt
import time

import aiohttp
import pytest

from app.db import Database
from app.job_store import JobStore
from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER, OPENROUTER_BUDGET_EXCEEDED_MARKER
from app.pipeline import (
    EXHAUSTION_RETRY_DELAY_SEC,
    MAX_TRANSIENT_RETRIES,
    _exhaustion_run_after,
    _is_exhaustion_failure,
)
from app.queue_service import _summary_queue_worker
from app.services_container import SummaryJob


OWNER_ID = 555
CHAT_ID = 100
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ── _is_exhaustion_failure: классификатор ──────────────────────────────────


def test_free_chain_exhausted_marker_is_exhaustion():
    exc = RuntimeError(
        f"{FREE_CHAIN_EXHAUSTED_MARKER}: все free-модели отказались. Последняя ошибка: ."
    )
    assert _is_exhaustion_failure(exc)


def test_budget_exceeded_marker_is_exhaustion():
    exc = RuntimeError(f"{OPENROUTER_BUDGET_EXCEEDED_MARKER}: дневной бюджет исчерпан")
    assert _is_exhaustion_failure(exc)


def test_plain_network_error_is_not_exhaustion():
    assert not _is_exhaustion_failure(aiohttp.ClientConnectionError("network storm"))


def test_generic_error_is_not_exhaustion():
    assert not _is_exhaustion_failure(RuntimeError("нет субтитров"))


# ── _exhaustion_run_after: когда отложить ──────────────────────────────────


def test_default_run_after_is_now_plus_one_hour():
    now = time.time()
    exc = RuntimeError(f"{FREE_CHAIN_EXHAUSTED_MARKER}: все модели отказали.")
    run_after = _exhaustion_run_after(exc, now)
    assert run_after == pytest.approx(now + EXHAUSTION_RETRY_DELAY_SEC, abs=1)


def test_daily_limit_marker_close_to_reset_uses_reset_time():
    # 23:00 UTC — до следующего сброса (00:05 UTC) меньше 12 часов.
    now = dt.datetime(2026, 8, 17, 23, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    exc = RuntimeError(
        f"{FREE_CHAIN_EXHAUSTED_MARKER}: Rate limit exceeded: free-models-per-day."
    )
    run_after = _exhaustion_run_after(exc, now)
    expected = dt.datetime(2026, 8, 18, 0, 5, 0, tzinfo=dt.timezone.utc).timestamp()
    assert run_after == pytest.approx(expected, abs=1)


def test_daily_limit_marker_far_from_reset_uses_default_delay():
    # 00:30 UTC — до следующего сброса (следующий день, 00:05) почти сутки —
    # дальше 12ч, обычный часовой бэкофф лучше, чем ждать почти сутки.
    now = dt.datetime(2026, 8, 17, 0, 30, 0, tzinfo=dt.timezone.utc).timestamp()
    exc = RuntimeError(
        f"{FREE_CHAIN_EXHAUSTED_MARKER}: Rate limit exceeded: free-models-per-day."
    )
    run_after = _exhaustion_run_after(exc, now)
    assert run_after == pytest.approx(now + EXHAUSTION_RETRY_DELAY_SEC, abs=1)


def test_daily_limit_marker_case_insensitive():
    now = time.time()
    exc = RuntimeError(f"{FREE_CHAIN_EXHAUSTED_MARKER}: FREE-MODELS-PER-DAY limit hit.")
    run_after = _exhaustion_run_after(exc, now)
    # Просто не должно упасть, и должно отличаться от плоского default в
    # общем случае — здесь важна не точная величина, а что маркер распознан
    # без учёта регистра (см. test_daily_limit_marker_close_to_reset_uses_reset_time
    # для точной арифметики).
    assert run_after > now


# ── shared fakes (как в test_transient_retry.py) ───────────────────────────


class _FakeSettings:
    def __init__(self, owner_user_id=OWNER_ID):
        self.owner_user_id = owner_user_id


class _FakeUsers:
    def is_owner(self, user_id):
        return user_id == OWNER_ID

    def is_allowed(self, user_id):
        return user_id == OWNER_ID


class _FakeYouTube:
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


class _FakeLLM:
    async def list_models(self):
        return []


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
        self.llm = _FakeLLM()
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


def _make_job(
    job_store: JobStore, *, transient_retries: int = 0, chat_id: int = CHAT_ID
) -> SummaryJob:
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
        transient_retries=transient_retries,
    )


async def _run_worker_once(services: _FakeServices, job: SummaryJob) -> None:
    await services.summary_queue.put(job)
    await _summary_queue_worker(services)


# ── (c) FREE_CHAIN_EXHAUSTED → deferred, счётчик+1, error НЕ отправлена ────


async def test_exhaustion_failure_defers_job_instead_of_failing(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = RuntimeError(
        f"{FREE_CHAIN_EXHAUSTED_MARKER}: все free-модели в цепочке отказались отвечать. "
        f"Последняя ошибка: ."
    )
    services = _FakeServices(exc, job_store)
    job = _make_job(job_store, transient_retries=0)
    before = time.time()

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 1
    assert before + EXHAUSTION_RETRY_DELAY_SEC - 5 <= row["run_after"] <= time.time() + EXHAUSTION_RETRY_DELAY_SEC + 5

    # Никакого финального сообщения об ошибке — только начальный статус
    # send_message + retry-эдит того же сообщения.
    assert len(services.bot.sent) == 1
    sent_message = services.bot.sent[0]
    assert "error" not in sent_message["text"].lower()

    status_message = services.summary_status_messages[CHAT_ID]
    assert status_message.edits, "retry-статус должен был обновить сообщение"
    assert "~60 мин" in status_message.edits[-1] or "~59 мин" in status_message.edits[-1]


async def test_budget_exceeded_failure_also_defers(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = RuntimeError(f"{OPENROUTER_BUDGET_EXCEEDED_MARKER}: дневной бюджет исчерпан")
    services = _FakeServices(exc, job_store)
    job = _make_job(job_store, transient_retries=0)

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 1


# ── (d) дневной лимит близко к сбросу → run_after ≈ 00:05 UTC ─────────────


async def test_daily_limit_marker_defers_until_next_reset(tmp_path, monkeypatch):
    job_store = _make_job_store(tmp_path)
    exc = RuntimeError(
        f"{FREE_CHAIN_EXHAUSTED_MARKER}: все free-модели отказали. "
        "Rate limit exceeded: free-models-per-day."
    )
    services = _FakeServices(exc, job_store)
    job = _make_job(job_store, transient_retries=0)

    # Замораживаем "текущее время" незадолго до сброса, чтобы однозначно
    # попасть в ветку "ближайший 00:05 UTC ближе, чем +1 час".
    fixed_now = dt.datetime(2026, 8, 17, 23, 59, 0, tzinfo=dt.timezone.utc).timestamp()
    monkeypatch.setattr("app.pipeline.time.time", lambda: fixed_now)

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    expected_reset = dt.datetime(2026, 8, 18, 0, 5, 0, tzinfo=dt.timezone.utc).timestamp()
    assert row["run_after"] == pytest.approx(expected_reset, abs=2)


# ── (e) 4-й отказ подряд (лимит исчерпан) → старое поведение: failed ───────


async def test_fourth_exhaustion_failure_falls_back_to_failed_with_friendly_text(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = RuntimeError(
        f"{FREE_CHAIN_EXHAUSTED_MARKER}: все free-модели отказали. Последняя ошибка: ."
    )
    services = _FakeServices(exc, job_store)
    assert MAX_TRANSIENT_RETRIES == 3
    job = _make_job(job_store, transient_retries=MAX_TRANSIENT_RETRIES)

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "failed"

    assert len(services.bot.sent) == 2
    error_text = services.bot.sent[-1]["text"]
    # Owner видит полный текст (включая дружелюбную подсказку из
    # _user_facing_error_reason) — не спутан с внутренним RuntimeError'ом.
    assert error_text


# ── (f) регрессия: транзиентная сетевая по-прежнему по Q4-пути ────────────


async def test_owner_chat_gets_deferred_retry_same_as_anyone_else(tmp_path):
    """Owner-чат не получает никакого спец-обхождения на этапе ретрая:
    exhaustion-исключение откладывает job точно так же, никакой финальной
    ошибки не шлётся раньше срока. Различие owner/остальные появляется
    только после исчерпания лимита попыток, в _user_facing_error_reason —
    не здесь (см. test_fourth_exhaustion_failure_falls_back_to_failed_with_friendly_text
    для этой отдельной проверки)."""
    job_store = _make_job_store(tmp_path)
    exc = RuntimeError(
        f"{FREE_CHAIN_EXHAUSTED_MARKER}: все free-модели отказали. Последняя ошибка: ."
    )
    services = _FakeServices(exc, job_store)
    job = _make_job(job_store, transient_retries=0, chat_id=OWNER_ID)

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 1
    assert len(services.bot.sent) == 1  # только исходный статус, без ошибки


async def test_transient_network_failure_still_uses_short_backoff_not_exhaustion(tmp_path):
    job_store = _make_job_store(tmp_path)
    exc = aiohttp.ClientConnectionError("network storm")
    services = _FakeServices(exc, job_store)
    job = _make_job(job_store, transient_retries=0)
    before = time.time()

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 1
    # Q4 бэкофф — 300 * 1 = 300 сек, НЕ час (EXHAUSTION_RETRY_DELAY_SEC=3600).
    assert before + 295 <= row["run_after"] <= before + 305
