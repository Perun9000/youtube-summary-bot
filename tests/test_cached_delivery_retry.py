"""R3: ретрай кэш-доставок при сетевом сбое.

Инцидент: «отправил и тишина» при сетевом моргании. Два независимых сбойных
узла (оба логировались, воспроизведены 25.08):
  (a) ручной путь — _send_cached_summary_to_chat из _enqueue_summary_job
      (queue_service): исключение улетало в хендлер, пользователь не получал
      НИЧЕГО (ни саммари, ни сообщения об ошибке).
  (b) local API — фоновая _deliver_cached_for_local_api
      (лог local_api.cached_delivery_failed): исключение просто терялось в
      логах, кнопка расширения выглядела так, будто всё ок.

Фикс: при СЕТЕВОЙ (транзиентной, см. app/pipeline.py::_is_transient_failure)
ошибке кэш-доставки — не терять её, а завести полноценный job через
job_store.add + сразу перевести в status='deferred' с run_after=+5 мин (та же
deferred-механика, что несёт премьеры и Q4-ретраи). run_deferred_jobs_scheduler
поднимет его, _process_youtube_job увидит top-of-job кэш-хит и доставит
саммари повторно. Нетранзиентные ошибки — поведение как было раньше
(проверяется отдельно, ничего нового не заводим).

Конвенции фейков — как в tests/test_queue_dedup.py и tests/test_transient_retry.py.
"""
from __future__ import annotations

import time

import aiohttp
import pytest
from aiogram.exceptions import TelegramNetworkError

import app.queue_service as queue_service
from app.queue_service import _deliver_cached_for_local_api, _enqueue_summary_job
from app.services_container import SummaryJob


CHAT_ID = 100
VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


# ── shared fakes ────────────────────────────────────────────────────────────


class _FakeCachedSummary:
    def __init__(self):
        self.video_id = VIDEO_ID
        self.url = URL
        self.telegraph_url = "https://telegra.ph/x"


class _FakeJobStore:
    def __init__(self):
        self.add_calls: list[dict] = []
        self.deferred_calls: list[dict] = []

    def add(self, url, chat_id, *, scheduled, disable_notification, title_hint, lang):
        self.add_calls.append({
            "url": url, "chat_id": chat_id, "scheduled": scheduled,
            "disable_notification": disable_notification,
            "title_hint": title_hint, "lang": lang,
        })
        return len(self.add_calls)

    def set_deferred(self, job_id, run_after, **kwargs):
        self.deferred_calls.append({"job_id": job_id, "run_after": run_after})

    def set_status(self, job_id, status):
        pass


class _FakeSettings:
    def __init__(self, owner_user_id=None):
        self.owner_user_id = owner_user_id


class _FakeUsers:
    def is_allowed(self, user_id):
        return True  # allowlisted → no quota gate in play for these tests


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeUser:
    def __init__(self, user_id, language_code="ru"):
        self.id = user_id
        self.language_code = language_code


class _FakeMessage:
    def __init__(self, chat_id, user_id, text=URL):
        self.chat = _FakeChat(chat_id)
        self.from_user = _FakeUser(user_id)
        self.text = text
        self.deleted = False
        self.answers = []

    async def delete(self):
        self.deleted = True

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        return object()


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return object()


class _FakeServices:
    def __init__(self, job_store):
        self.settings = _FakeSettings()
        self.users = _FakeUsers()
        self.billing = None
        self.quota = None
        self.summary_cache = None  # bypassed — _lookup_cached_summary is monkeypatched
        self.job_store = job_store
        self.user_langs = None
        self.bot = _FakeBot()
        self.summary_queue = __import__("asyncio").PriorityQueue()
        self.summary_queue_lock = __import__("asyncio").Lock()
        self.summary_worker_task = None
        self.summary_next_sequence = 0
        self.summary_active_job = None
        self.summary_status_messages = {}
        self.summary_status_base_texts = {}
        self.summary_status_parse_modes = {}
        self.summary_status_disable_previews = {}


def _network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=None, message="Connection reset by peer")


# ── (a) manual path: transient failure defers instead of crashing ─────────


async def test_cached_delivery_transient_failure_defers_instead_of_raising(monkeypatch):
    cached = _FakeCachedSummary()
    monkeypatch.setattr(queue_service, "_lookup_cached_summary", lambda url, services, lang="ru": cached)

    async def failing_send(message, cached_arg, services):
        raise _network_error()

    monkeypatch.setattr(queue_service, "_send_cached_summary_to_chat", failing_send)

    job_store = _FakeJobStore()
    services = _FakeServices(job_store)
    message = _FakeMessage(chat_id=CHAT_ID, user_id=999)
    before = time.time()

    # Must NOT raise — this is exactly what left the user with nothing.
    await _enqueue_summary_job(message, URL, services)

    assert len(job_store.add_calls) == 1
    assert job_store.add_calls[0]["url"] == URL
    assert job_store.add_calls[0]["chat_id"] == CHAT_ID
    assert len(job_store.deferred_calls) == 1
    run_after = job_store.deferred_calls[0]["run_after"]
    assert before + 295 <= run_after <= time.time() + 305
    # Original link message still gets cleaned up — a proper delivery is
    # coming later via the deferred job, same as any other enqueue.
    assert message.deleted is True


async def test_cached_delivery_non_transient_failure_behaves_as_before(monkeypatch):
    cached = _FakeCachedSummary()
    monkeypatch.setattr(queue_service, "_lookup_cached_summary", lambda url, services, lang="ru": cached)

    async def failing_send(message, cached_arg, services):
        raise RuntimeError("not a network problem")

    monkeypatch.setattr(queue_service, "_send_cached_summary_to_chat", failing_send)

    job_store = _FakeJobStore()
    services = _FakeServices(job_store)
    message = _FakeMessage(chat_id=CHAT_ID, user_id=999)

    with pytest.raises(RuntimeError):
        await _enqueue_summary_job(message, URL, services)

    # No deferred retry created for a non-transient failure — old behavior.
    assert job_store.add_calls == []
    assert job_store.deferred_calls == []


# ── (b) local_api path: same transient/non-transient split ────────────────


def _make_probe_job() -> SummaryJob:
    return SummaryJob(
        sequence=0, message=None, url=URL, enqueued_at=time.monotonic(),
        chat_id=CHAT_ID, lang="ru",
    )


async def test_local_api_cached_delivery_transient_failure_defers(monkeypatch):
    cached = _FakeCachedSummary()

    async def failing_deliver(job_arg, services_arg, cached_arg):
        raise aiohttp.ClientConnectionError("network storm")

    monkeypatch.setattr(queue_service, "_deliver_cached_summary_for_job", failing_deliver)

    job_store = _FakeJobStore()
    services = _FakeServices(job_store)
    job = _make_probe_job()

    # Must NOT raise — this runs detached via asyncio.create_task in prod.
    await _deliver_cached_for_local_api(job, services, cached, VIDEO_ID)

    assert len(job_store.add_calls) == 1
    assert job_store.add_calls[0]["url"] == URL
    assert job_store.add_calls[0]["chat_id"] == CHAT_ID
    assert len(job_store.deferred_calls) == 1


async def test_local_api_cached_delivery_non_transient_failure_only_logs(monkeypatch, caplog):
    cached = _FakeCachedSummary()

    async def failing_deliver(job_arg, services_arg, cached_arg):
        raise RuntimeError("not a network problem")

    monkeypatch.setattr(queue_service, "_deliver_cached_summary_for_job", failing_deliver)

    job_store = _FakeJobStore()
    services = _FakeServices(job_store)
    job = _make_probe_job()

    with caplog.at_level("ERROR"):
        # Must NOT raise — old behavior (logger.exception + swallow) preserved.
        await _deliver_cached_for_local_api(job, services, cached, VIDEO_ID)

    assert job_store.add_calls == []
    assert job_store.deferred_calls == []
    assert any("cached_delivery_failed" in record.message for record in caplog.records)
