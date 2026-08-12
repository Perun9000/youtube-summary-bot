"""Q6: статус-сообщения best-effort.

Инцидент 2026-08-12: сетевой шторм VPS→Telegram. Обновление статус-сообщения
провисело 60с (дефолтный таймаут aiogram) и упало TelegramNetworkError на
участке воркера ДО старта пайплайна (app/pipeline.py::_process_youtube_job,
самый первый _set_service_status(status.fetching) вызов — ДО try:, значит
мимо Q4-ретрая) → job failed без Q4-ретрая, error-сообщение тоже не доехало —
для пользователя «бот остановился», хотя генерация даже не начиналась.

Косметика (статусные сообщения) не имеет права ронять задачу или блокировать
её на минуту. Существенные отправки (саммари, ошибка генерации, quota-denied)
не тронуты — их семантика прежняя.

Конвенции фейков — как в tests/test_queue_dedup.py и tests/test_transient_retry.py:
минимальные классы-заглушки вместо полноценных Services/Message.
"""
import asyncio
import time

import pytest
from aiogram.exceptions import TelegramNetworkError

import app.status_messages as status_messages
from app.services_container import SummaryJob
from app.status_messages import (
    _delete_message_safely,
    _set_service_status,
)


CHAT_ID = 100
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ── shared fakes ────────────────────────────────────────────────────────────


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    """edit_text / delete raise whatever is queued in ``edit_error`` /
    ``delete_error`` — None means "succeed normally"."""

    def __init__(self, chat_id=CHAT_ID):
        self.chat = _FakeChat(chat_id)
        self.edits = []
        self.deleted = False
        self.edit_error = None
        self.delete_error = None

    async def edit_text(self, text, **kwargs):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(text)
        return self

    async def delete(self):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True

    async def answer(self, text, **kwargs):
        return self


class _FakeBot:
    def __init__(self, send_error=None):
        self.sent = []
        self.send_error = send_error

    async def send_message(self, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(kwargs)
        return _FakeMessage(chat_id=kwargs["chat_id"])


class _FakeServices:
    def __init__(self, *, bot=None):
        self.bot = bot if bot is not None else _FakeBot()
        self.summary_queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
        self.summary_queue_lock = asyncio.Lock()
        self.summary_active_job = None
        self.summary_status_messages = {}
        self.summary_status_base_texts = {}
        self.summary_status_parse_modes = {}
        self.summary_status_disable_previews = {}


def _make_job(chat_id=CHAT_ID) -> SummaryJob:
    return SummaryJob(
        sequence=1, message=None, url=URL, enqueued_at=time.monotonic(),
        chat_id=chat_id, lang="ru",
    )


def _network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=None, message="Connection reset by peer")


# ── (a) send_message кидает TelegramNetworkError → None, не проброшено ────


async def test_set_service_status_absorbs_network_error_on_send(caplog):
    bot = _FakeBot(send_error=_network_error())
    services = _FakeServices(bot=bot)
    job = _make_job()

    with caplog.at_level("WARNING"):
        result = await _set_service_status(
            services=services, source_message=None, text="Получаю данные...", job=job,
        )

    assert result is None
    assert any(
        "status.update_failed" in r.message and str(CHAT_ID) in r.message
        for r in caplog.records
    )


# ── (b) edit кидает сетевую → старое сообщение НЕ удалено, словари не потеряны ─


async def test_set_service_status_edit_network_error_keeps_old_message_and_state():
    services = _FakeServices()
    job = _make_job()
    old_message = _FakeMessage(chat_id=CHAT_ID)
    old_message.edit_error = _network_error()
    services.summary_status_messages[CHAT_ID] = old_message
    services.summary_status_base_texts[CHAT_ID] = "Получаю данные..."
    services.summary_status_parse_modes[CHAT_ID] = None
    services.summary_status_disable_previews[CHAT_ID] = False

    result = await _set_service_status(
        services=services, source_message=None, text="Генерирую summary...", job=job,
    )

    assert result is None
    # Старое сообщение НЕ удалено (сетевой сбой на edit — не "сообщение
    # устарело", ретраить нужно на ТОМ ЖЕ сообщении).
    assert old_message.deleted is False
    # Ссылка на старое сообщение не потеряна — следующий апдейт попробует
    # снова именно на нём.
    assert services.summary_status_messages[CHAT_ID] is old_message
    # Ни один из словарей summary_status_* не обнулился/не потерял запись.
    assert CHAT_ID in services.summary_status_base_texts
    assert CHAT_ID in services.summary_status_parse_modes
    assert CHAT_ID in services.summary_status_disable_previews
    # send_message не вызывался — после сетевого сбоя на edit'е НЕ шлём
    # запасное новое сообщение (иначе в чате задвоится статус).
    assert services.bot.sent == []


# ── (c) delete_message_safely: сетевой сбой поглощается ────────────────────


async def test_delete_message_safely_absorbs_network_error(caplog):
    message = _FakeMessage()
    message.delete_error = _network_error()

    with caplog.at_level("WARNING"):
        await _delete_message_safely(message)  # не должно бросить

    assert any("status.update_failed" in r.message for r in caplog.records) or any(
        "status.delete.failed" in r.message for r in caplog.records
    )


# ── (d) таймаут: вызов висит > 5с → поглощён за ~5с, не 60 ─────────────────


async def test_set_service_status_send_times_out_quickly(monkeypatch, caplog):
    monkeypatch.setattr(status_messages, "_STATUS_IO_TIMEOUT_SEC", 0.05)

    class _HangingBot:
        async def send_message(self, **kwargs):
            await asyncio.sleep(10)  # would hang far longer than the timeout

    services = _FakeServices(bot=_HangingBot())
    job = _make_job()

    started = time.monotonic()
    with caplog.at_level("WARNING"):
        result = await _set_service_status(
            services=services, source_message=None, text="Получаю данные...", job=job,
        )
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 2.0  # absorbed at the patched ~0.05s budget, not 10s
    assert any("status.update_failed" in r.message for r in caplog.records)


# ── (e) воркер-сценарий: статус упал по сети → job ПРОДОЛЖАЕТ обработку ───


class _FakeJobStore:
    def __init__(self):
        self.statuses = {}

    def set_status(self, db_id, status):
        self.statuses[db_id] = status


class _FakeYouTube:
    """Non-transient failure AFTER the initial status call — its call count
    proves the worker reached the try: block (i.e. survived the crashing
    status update at app/pipeline.py:384, which sits BEFORE try:)."""

    def __init__(self):
        self.calls = 0

    def fetch_metadata(self, url):
        self.calls += 1
        raise RuntimeError("нет субтитров")  # classified non-transient


class _FakeSettings:
    def __init__(self):
        self.owner_user_id = 999999


class _FakeUsers:
    def is_owner(self, user_id):
        return False

    def is_allowed(self, user_id):
        return False


class _FakeLLM:
    async def list_models(self):
        return []


class _WorkerFakeBot:
    """send_message fails on the FIRST call (the "fetching" status — the
    exact call that crashed in the incident) and recovers afterwards, like a
    real network storm that clears up a few seconds later."""

    def __init__(self):
        self.calls = 0
        self.sent = []

    async def send_message(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise TelegramNetworkError(method=None, message="Connection reset by peer")
        self.sent.append(kwargs)
        return _FakeMessage(chat_id=kwargs["chat_id"])


class _FakeServicesForWorker:
    def __init__(self):
        self.settings = _FakeSettings()
        self.users = _FakeUsers()
        self.billing = None
        self.quota = None
        self.youtube = _FakeYouTube()
        self.summary_cache = None
        self.job_store = _FakeJobStore()
        self.bot = _WorkerFakeBot()
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


async def test_worker_survives_network_failure_on_initial_status_update():
    from app.queue_service import _summary_queue_worker

    services = _FakeServicesForWorker()
    job = SummaryJob(
        sequence=1, message=None, url=URL, enqueued_at=time.monotonic(),
        chat_id=CHAT_ID, db_id=42, lang="ru",
    )
    await services.summary_queue.put(job)

    await _summary_queue_worker(services)  # must not raise TelegramNetworkError

    # Before the fix: the crash happened at the "fetching" status call,
    # BEFORE fetch_metadata was ever reached — job went straight to failed
    # via the worker's outer except, with zero fetch_metadata calls and zero
    # further Telegram sends. After the fix, the status crash is absorbed
    # and processing proceeds into the real try: block.
    assert services.youtube.calls == 1
    assert services.job_store.statuses.get(42) == "failed"
    # The network storm cleared after the first call in this test (as it did
    # in the real incident a few seconds later) — subsequent Telegram sends
    # (interrupted-status + final user-facing error message) DID get through,
    # proving the cosmetic status crash didn't take substantive delivery
    # down with it.
    assert services.bot.calls >= 2
    assert len(services.bot.sent) >= 1
