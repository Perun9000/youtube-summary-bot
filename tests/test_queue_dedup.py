"""Q2, Fix 2: дедупликация очереди.

Несколько кликов по одному ролику (в одном чате) не должны ставить несколько
job'ов — иначе внешнему пользователю списалась бы квота за каждый клик, и
воркер бы дважды генерировал одно и то же саммари.

Конвенции фейков — как в tests/test_local_api_enqueue.py и
tests/test_priority_queue.py: минимальные классы-заглушки вместо полноценных
Services/Message.

Q2, Fix 1 (частично): статус-сообщения для job'ов без aiogram Message
(enqueue_local_api_job) и снятие гейта job.scheduled в _set_service_status /
_refresh_active_service_status.
"""
import asyncio

import app.queue_service as queue_service
from app.queue_service import (
    PRIORITY_FREE,
    PRIORITY_PRIVILEGED,
    _enqueue_summary_job,
    _find_duplicate_job,
    enqueue_local_api_job,
)
from app.services_container import SummaryJob
from app.status_messages import _set_service_status


OWNER_ID = 5779821
CHAT_A = 100
CHAT_B = 200
VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
OTHER_VIDEO_ID = "AbCdEfGhIjK"


# ── shared fakes ───────────────────────────────────────────────────────────


class _FakeSettings:
    def __init__(self, owner_user_id=OWNER_ID):
        self.owner_user_id = owner_user_id


class _FakeJobStore:
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


class _FakeUsers:
    def __init__(self, allowed_ids=()):
        self._allowed = set(allowed_ids)

    def is_allowed(self, user_id):
        return user_id is not None and user_id in self._allowed


class _FakeQuotaVerdict:
    def __init__(self, allowed=True, remaining=10, deny_reason=None):
        self.allowed = allowed
        self.remaining = remaining
        self.deny_reason = deny_reason


class _FakeQuota:
    """Records check() calls so tests can assert quota was NOT charged for a
    duplicate — the whole point of running the dedup check before the quota
    gate."""

    def __init__(self, allowed=True):
        self.calls = []
        self._allowed = allowed

    def check(self, user_id, weight=1):
        self.calls.append((user_id, weight))
        return _FakeQuotaVerdict(allowed=self._allowed)


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return object()


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


class _FakeServices:
    """Shared fake Services for both the manual (_enqueue_summary_job) and
    local API (enqueue_local_api_job) enqueue paths."""

    def __init__(self, *, allowed_ids=(), quota=None, user_langs=None):
        self.settings = _FakeSettings()
        self.users = _FakeUsers(allowed_ids)
        self.billing = None
        self.quota = quota
        self.summary_cache = None  # cache miss on every lookup
        self.job_store = _FakeJobStore()
        self.user_langs = user_langs
        self.summary_queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
        self.summary_queue_lock = asyncio.Lock()
        self.summary_worker_task = None
        self.summary_next_sequence = 0
        self.summary_active_job = None
        # transcription_queue/lock deliberately absent (getattr default None
        # in _find_duplicate_job) — matches a Services instance that hasn't
        # initialized the transcription queue yet.
        self.bot = _FakeBot()
        self.summary_status_messages = {}
        self.summary_status_base_texts = {}
        self.summary_status_parse_modes = {}
        self.summary_status_disable_previews = {}


async def _stop_worker(services: _FakeServices) -> None:
    task = services.summary_worker_task
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _seed_job(services: _FakeServices, *, url=URL, chat_id=CHAT_A, priority=PRIORITY_FREE, sequence=1) -> SummaryJob:
    job = SummaryJob(
        sequence=sequence, message=None, url=url,
        enqueued_at=0.0, chat_id=chat_id, priority=priority, lang="ru",
    )
    services.summary_queue.put_nowait(job)
    return job


# ── (a) второй enqueue того же (video, chat) не создаёт новый job ─────────


async def test_enqueue_summary_job_skips_duplicate_same_video_same_chat():
    quota = _FakeQuota()
    services = _FakeServices(allowed_ids=(), quota=quota)  # external user → quota applies
    existing = _seed_job(services, chat_id=CHAT_A)

    message = _FakeMessage(chat_id=CHAT_A, user_id=999)
    await _enqueue_summary_job(message, URL, services)

    # Ничего нового не добавлено: очередь по-прежнему содержит только seed-job.
    assert services.summary_queue.qsize() == 1
    assert services.job_store.calls == []  # job_store.add НЕ вызывался
    assert quota.calls == []  # квота НЕ списана за дубль
    assert message.answers == ["Этот ролик уже в очереди — саммари придёт, как только он обработается."]
    assert message.deleted is True  # чат остаётся чистым, как при обычной постановке

    remaining = services.summary_queue.get_nowait()
    assert remaining is existing


# ── _find_duplicate_job: transcription-очередь и битые URL ────────────────


async def test_find_duplicate_job_checks_transcription_queue_too():
    """Ролик мог уйти в transcription_queue (нет субтитров, ждёт Groq) — он
    всё ещё "в очереди" с точки зрения дедупа, хоть и не в summary_queue."""
    services = _FakeServices()
    services.transcription_queue = asyncio.Queue()
    services.transcription_queue_lock = asyncio.Lock()
    services.transcription_active_job = None
    stuck = SummaryJob(
        sequence=1, message=None, url=URL, enqueued_at=0.0, chat_id=CHAT_A, lang="ru",
    )
    services.transcription_queue.put_nowait(stuck)

    found = await _find_duplicate_job(VIDEO_ID, CHAT_A, services)

    assert found is stuck


async def test_find_duplicate_job_ignores_candidate_with_unparseable_url():
    services = _FakeServices()
    _seed_job(services, url="not-a-youtube-url", chat_id=CHAT_A)

    found = await _find_duplicate_job(VIDEO_ID, CHAT_A, services)

    assert found is None


# ── (b) другой chat с тем же video — НЕ дубль ──────────────────────────────


async def test_enqueue_summary_job_different_chat_same_video_is_not_duplicate():
    quota = _FakeQuota()
    services = _FakeServices(allowed_ids=(), quota=quota)
    _seed_job(services, chat_id=CHAT_A)

    message = _FakeMessage(chat_id=CHAT_B, user_id=888)
    await _enqueue_summary_job(message, URL, services)

    assert services.summary_queue.qsize() == 2  # seed-job + новый job для CHAT_B
    assert len(services.job_store.calls) == 1
    assert services.job_store.calls[0]["chat_id"] == CHAT_B
    assert quota.calls  # обычная (не дублирующая) постановка — квота проверяется
    assert message.deleted is True

    await _stop_worker(services)


# ── (c) local API дубль → "queued" без второго job ────────────────────────


async def test_enqueue_local_api_job_skips_duplicate():
    services = _FakeServices()
    _seed_job(services, chat_id=OWNER_ID, priority=PRIORITY_PRIVILEGED)

    result = await enqueue_local_api_job(VIDEO_ID, services)

    assert result == "queued"
    assert services.summary_queue.qsize() == 1  # только seed-job, второй не создан
    assert services.job_store.calls == []
    assert services.bot.sent == []  # дубль не шлёт статус повторно


async def test_enqueue_local_api_job_different_video_is_not_duplicate():
    services = _FakeServices()
    _seed_job(services, url=f"https://www.youtube.com/watch?v={OTHER_VIDEO_ID}", chat_id=OWNER_ID, priority=PRIORITY_PRIVILEGED)

    result = await enqueue_local_api_job(VIDEO_ID, services)

    assert result == "queued"
    assert services.summary_queue.qsize() == 2
    assert len(services.job_store.calls) == 1

    await _stop_worker(services)


# ── (d) enqueue_local_api_job шлёт начальный статус ────────────────────────


async def test_enqueue_local_api_job_sends_initial_status(monkeypatch):
    sent = []

    async def fake_set_service_status(services, source_message, text, job=None, **kwargs):
        sent.append({"source_message": source_message, "text": text, "job": job})
        return object()

    monkeypatch.setattr(queue_service, "_set_service_status", fake_set_service_status)
    services = _FakeServices()

    result = await enqueue_local_api_job(VIDEO_ID, services)

    assert result == "queued"
    assert len(sent) == 1
    assert sent[0]["source_message"] is None
    assert sent[0]["job"].url == URL
    assert sent[0]["text"] == "Добавил ролик в очередь summary. Начинаю обработку."

    await _stop_worker(services)


# ── (e) _set_service_status с message=None и НЕ-scheduled job шлёт через bot ─


async def test_set_service_status_sends_via_bot_for_non_scheduled_message_none_job():
    """До фикса Q2 этот путь требовал job.scheduled — восстановленные после
    рестарта задачи и job'ы от локального API (message=None, scheduled=False)
    молчали. Гейт снят: единственное условие теперь — job.chat_id и
    services.bot."""
    services = _FakeServices()
    job = SummaryJob(
        sequence=1, message=None, url=URL,
        enqueued_at=0.0, chat_id=OWNER_ID, lang="ru", scheduled=False,
    )

    result = await _set_service_status(
        services=services,
        source_message=None,
        text="Генерирую summary...",
        job=job,
    )

    assert result is not None
    assert len(services.bot.sent) == 1
    assert services.bot.sent[0]["chat_id"] == OWNER_ID
    assert services.summary_status_messages[OWNER_ID] is result


async def test_set_service_status_returns_none_without_bot_or_message():
    services = _FakeServices()
    services.bot = None
    job = SummaryJob(
        sequence=1, message=None, url=URL,
        enqueued_at=0.0, chat_id=OWNER_ID, lang="ru", scheduled=False,
    )

    result = await _set_service_status(
        services=services,
        source_message=None,
        text="Генерирую summary...",
        job=job,
    )

    assert result is None
    assert services.summary_status_messages == {}
