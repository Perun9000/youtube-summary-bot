import asyncio

import pytest

import app.queue_service as queue_service
from app.queue_service import enqueue_local_api_job


OWNER_ID = 5779821
VIDEO_ID = "dQw4w9WgXcQ"


class _FakeSettings:
    def __init__(self, owner_user_id):
        self.owner_user_id = owner_user_id


class _FakeJobStore:
    """Records add() calls; set_status is a no-op (matches JobStore's shape
    closely enough for the worker task that may be scheduled but never gets
    to run before asyncio.run() tears down the loop)."""

    def __init__(self):
        self.calls = []

    def add(self, url, chat_id, *, scheduled, disable_notification, title_hint, lang):
        self.calls.append({
            "url": url, "chat_id": chat_id, "scheduled": scheduled,
            "disable_notification": disable_notification,
            "title_hint": title_hint, "lang": lang,
        })
        return len(self.calls)

    def set_status(self, db_id, status):
        pass


class _FakeUserLangs:
    def __init__(self, mapping=None):
        self._mapping = mapping or {}

    def get(self, user_id):
        return self._mapping.get(user_id)


class _FakeSummaryCache:
    def __init__(self, cached_obj=None):
        self._cached_obj = cached_obj

    def get(self, video_id, lang="ru"):
        return self._cached_obj


class _FakeBot:
    """Records send_message calls; enqueue_local_api_job now sends an initial
    queue status via _set_service_status(source_message=None), which needs a
    bot to send through (see app/status_messages.py)."""

    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return object()


class _FakeServices:
    def __init__(self, owner_user_id=OWNER_ID, user_langs=None, summary_cache=None):
        self.settings = _FakeSettings(owner_user_id)
        self.summary_queue = asyncio.PriorityQueue()
        self.summary_queue_lock = asyncio.Lock()
        self.summary_worker_task = None
        self.summary_next_sequence = 0
        self.summary_active_job = None
        self.job_store = _FakeJobStore()
        self.user_langs = user_langs
        self.summary_cache = summary_cache
        self.bot = _FakeBot()
        self.summary_status_messages = {}
        self.summary_status_base_texts = {}
        self.summary_status_parse_modes = {}
        self.summary_status_disable_previews = {}


async def _stop_worker(services: _FakeServices) -> None:
    """enqueue_local_api_job spawns _summary_queue_worker as a background
    task. The test drains the queue synchronously (get_nowait) before the
    worker gets a chance to run, so it just needs to be cancelled and
    awaited — otherwise it's left pending when the test's event loop closes
    and pytest reports an unraisable-exception warning."""
    task = services.summary_worker_task
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_enqueue_local_api_job_queues_for_owner():
    services = _FakeServices(
        user_langs=_FakeUserLangs({OWNER_ID: ("ru", "manual")}),
        summary_cache=None,  # empty cache => no cache hit
    )

    result = await enqueue_local_api_job(VIDEO_ID, services)

    assert result == "queued"
    job = services.summary_queue.get_nowait()
    assert job.message is None
    assert job.chat_id == OWNER_ID
    assert job.quota_user_id is None  # owner — unlimited
    assert job.url == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert job.lang == "ru"

    await _stop_worker(services)


async def test_enqueue_local_api_job_defaults_lang_ru_without_user_langs():
    services = _FakeServices(user_langs=None, summary_cache=None)

    result = await enqueue_local_api_job(VIDEO_ID, services)

    assert result == "queued"
    job = services.summary_queue.get_nowait()
    assert job.lang == "ru"

    await _stop_worker(services)


async def test_enqueue_local_api_job_cache_hit_returns_cached(monkeypatch):
    delivered = []

    async def fake_deliver(job, services, cached):
        delivered.append((job, services, cached))

    monkeypatch.setattr(queue_service, "_deliver_cached_summary_for_job", fake_deliver)

    sentinel_cached = object()
    services = _FakeServices(
        user_langs=_FakeUserLangs({OWNER_ID: ("ru", "manual")}),
        summary_cache=_FakeSummaryCache(sentinel_cached),
    )

    result = await enqueue_local_api_job(VIDEO_ID, services)

    assert result == "cached"
    assert services.summary_queue.qsize() == 0
    await asyncio.sleep(0)  # доставка идёт фоновой задачей — дать ей тик
    assert len(delivered) == 1
    assert delivered[0][2] is sentinel_cached


async def test_enqueue_local_api_job_cache_hit_does_not_block_on_delivery(monkeypatch):
    """Кэш-хит должен отвечать мгновенно: доставка в Telegram — это несколько
    bot.send_message (секунды), а расширение ждёт HTTP-ответ всего 1.5 сек.
    Медленная доставка обязана уходить в фоновую asyncio-задачу, иначе кнопка
    по таймауту откатывается на deep-link и Telegram открывается зря."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_deliver(job, services, cached):
        started.set()
        await release.wait()

    monkeypatch.setattr(queue_service, "_deliver_cached_summary_for_job", slow_deliver)
    services = _FakeServices(
        user_langs=_FakeUserLangs({OWNER_ID: ("ru", "manual")}),
        summary_cache=_FakeSummaryCache(object()),
    )

    result = await asyncio.wait_for(enqueue_local_api_job(VIDEO_ID, services), timeout=0.5)

    assert result == "cached"
    await asyncio.wait_for(started.wait(), timeout=1)  # доставка реально стартовала
    release.set()
    await asyncio.sleep(0)  # дать фоновой задаче завершиться до teardown'а


async def test_enqueue_local_api_job_without_owner_raises():
    services = _FakeServices(owner_user_id=None)

    with pytest.raises(RuntimeError, match="owner_not_configured"):
        await enqueue_local_api_job(VIDEO_ID, services)
