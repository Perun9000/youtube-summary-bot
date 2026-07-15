"""Q1: приоритетная очередь саммари.

Три ранга приоритета в summary_queue (SummaryJob.priority): 0 — owner/
allowlist/платные подписчики, 1 — внешние free-пользователи, 2 — scheduled-
мониторинг каналов. Внутри ранга — FIFO по sequence. Уже начатую задачу
приоритет не вытесняет — выбор действует только среди ожидающих.

Конвенции фейков — как в tests/test_local_api_enqueue.py: минимальные
классы-заглушки вместо полноценных Services/Message.
"""
import asyncio

from app.queue_service import PRIORITY_FREE, PRIORITY_PRIVILEGED, PRIORITY_SCHEDULED, _job_priority
from app.services_container import SummaryJob
from app.status_messages import _queue_block


def _job(sequence: int, priority: int, chat_id: int = 1, lang: str = "ru") -> SummaryJob:
    return SummaryJob(
        sequence=sequence,
        message=None,
        url=f"https://www.youtube.com/watch?v=video{sequence:03d}",
        enqueued_at=0.0,
        chat_id=chat_id,
        priority=priority,
        lang=lang,
    )


# ── (a) scheduled уже в очереди, приходит free → free первым ──────────────


async def test_free_job_overtakes_already_queued_scheduled_job():
    queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
    scheduled_job = _job(sequence=1, priority=PRIORITY_SCHEDULED)
    await queue.put(scheduled_job)

    free_job = _job(sequence=2, priority=PRIORITY_FREE)
    await queue.put(free_job)

    next_job = queue.get_nowait()

    assert next_job is free_job


# ── (b) free в очереди, приходит подписчик/owner → подписчик первым ───────


async def test_privileged_job_overtakes_already_queued_free_job():
    queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
    free_job = _job(sequence=1, priority=PRIORITY_FREE)
    await queue.put(free_job)

    privileged_job = _job(sequence=2, priority=PRIORITY_PRIVILEGED)
    await queue.put(privileged_job)

    next_job = queue.get_nowait()

    assert next_job is privileged_job


# ── (c) равный приоритет → FIFO по sequence, порядок постановки не важен ──


async def test_equal_priority_jobs_are_fifo_by_sequence():
    queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
    later = _job(sequence=5, priority=PRIORITY_FREE)
    earlier = _job(sequence=3, priority=PRIORITY_FREE)
    # Постановка в очередь в "неправильном" порядке (позже-возникший
    # sequence кладём первым) — сортировать должен приоритет+sequence, а
    # не порядок put().
    await queue.put(later)
    await queue.put(earlier)

    next_job = queue.get_nowait()

    assert next_job is earlier
    assert queue.get_nowait() is later


# ── (d) position в статусе учитывает вклинивание приоритета ───────────────


async def test_queue_block_position_reflects_priority_not_heap_insertion_order():
    """heapq хранит только heap-инвариант, не полный порядок: для трёх
    job'ов, вставленных в порядке free(seq=1), free(seq=2), privileged(seq=3),
    внутренний список ._queue НЕ будет отсортирован как [privileged, free1,
    free2] естественным образом при наивном обходе — _queue_block обязан
    сортировать по (priority, sequence) сам, иначе позиция и список "своих"
    роликов разъедутся с тем, что реально возьмёт воркер следующим."""
    services = _FakeServicesForQueueBlock()
    job_a = _job(sequence=1, priority=PRIORITY_FREE, chat_id=100)
    job_b = _job(sequence=2, priority=PRIORITY_FREE, chat_id=100)
    job_c = _job(sequence=3, priority=PRIORITY_PRIVILEGED, chat_id=200)

    await services.summary_queue.put(job_a)
    await services.summary_queue.put(job_b)
    await services.summary_queue.put(job_c)

    # Правильный порядок обработки: C (priority 0), затем A (priority 1,
    # seq 1), затем B (priority 1, seq 2). Позиция A в этом порядке — 2-я.
    text = await _queue_block(services, job_a)

    # status.queue_line (ru): "очередь: {position}/{total}"
    assert text.splitlines()[0] == "очередь: 2/3"


async def test_queue_block_lists_only_same_chat_pending_jobs_in_priority_order():
    services = _FakeServicesForQueueBlock()
    mine_low_priority = _job(sequence=1, priority=PRIORITY_FREE, chat_id=100)
    mine_high_priority = _job(sequence=2, priority=PRIORITY_PRIVILEGED, chat_id=100)
    someone_elses = _job(sequence=3, priority=PRIORITY_PRIVILEGED, chat_id=200)

    # Вставляем так, чтобы "своя" высокоприоритетная задача физически легла
    # в heap после низкоприоритетной — сортировка должна это поправить.
    await services.summary_queue.put(mine_low_priority)
    await services.summary_queue.put(someone_elses)
    await services.summary_queue.put(mine_high_priority)

    text = await _queue_block(services, mine_low_priority)
    lines = text.splitlines()

    # Порядок строк со "своими" роликами должен идти по приоритету:
    # высокоприоритетный раньше низкоприоритетного, даже если тот лёг в
    # очередь позже.
    own_lines = [line for line in lines if line.startswith("-")]
    assert len(own_lines) == 1
    assert "video002" in own_lines[0]  # mine_high_priority — единственный "свой", кроме job'а самого статуса


class _FakeServicesForQueueBlock:
    def __init__(self):
        self.summary_queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
        self.summary_queue_lock = asyncio.Lock()
        self.summary_active_job = None


# ── _job_priority: классификация owner/allowlist/подписчик/free ──────────


class _FakeUsers:
    def __init__(self, allowed_ids):
        self._allowed = set(allowed_ids)

    def is_allowed(self, user_id):
        return user_id is not None and user_id in self._allowed


class _FakeBilling:
    def __init__(self, subscriber_ids):
        self._subs = set(subscriber_ids)

    def is_subscriber(self, user_id, now=None):
        return user_id in self._subs


class _FakeServicesForPriority:
    def __init__(self, allowed_ids=(), subscriber_ids=()):
        self.users = _FakeUsers(allowed_ids)
        self.billing = _FakeBilling(subscriber_ids)


def test_job_priority_owner_or_allowlist_is_privileged():
    services = _FakeServicesForPriority(allowed_ids={42})
    assert _job_priority(42, services) == PRIORITY_PRIVILEGED


def test_job_priority_subscriber_is_privileged():
    services = _FakeServicesForPriority(allowed_ids=(), subscriber_ids={99})
    assert _job_priority(99, services) == PRIORITY_PRIVILEGED


def test_job_priority_external_free_user_is_free():
    services = _FakeServicesForPriority(allowed_ids=(), subscriber_ids=())
    assert _job_priority(7, services) == PRIORITY_FREE
