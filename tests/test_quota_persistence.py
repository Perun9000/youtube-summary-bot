"""R5: персист quota_user_id для восстановленных job'ов.

Pre-existing дыра (ревью 2026-08-25): jobs-таблица не хранила quota_user_id —
ЛЮБОЙ восстановленный/deferred job (рестарт, премьеры, Q4/Q8/Q12/Q13-ретраи,
R3-кэш-деферралы) пересоздавался с quota_user_id=None, так что квота внешнего
пользователя переставала проверяться и списываться (оба гейта в pipeline.py —
тяжёлый Groq-гейт и финальный charge — под ``if quota_user_id is not None``).

Фикс: колонка ``jobs.quota_user_id`` (nullable) + персист в JobStore.add +
перенос в restore_pending_jobs/_requeue_due_deferred. R3-путь (кэш-доставка)
покрыт отдельно в tests/test_cached_delivery_retry.py — там же, где заводится
``_defer_lost_cached_delivery``.

Конвенции фейков — как в tests/test_transient_retry.py.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app.db import Database
from app.job_store import JobStore
from app.monitoring_config import MonitoredChannel
from app.monitoring_rss import FeedEntry
from app.monitoring_service import ScheduledCandidate
from app.models import VideoMetadata
from app.queue_service import (
    _requeue_due_deferred,
    enqueue_scheduled_candidate,
    restore_pending_jobs,
)
from app.services_container import SummaryJob


CHAT_ID = 100
EXTERNAL_USER_ID = 777
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ── (a) миграция: старая БД без quota_user_id получает колонку ────────────


def test_migration_adds_quota_user_id_column_to_old_db(tmp_path):
    db_path = tmp_path / "bot.db"
    # Строим "старую" БД — jobs без quota_user_id, ровно как таблица
    # выглядела до R5 (см. _SCHEMA в app/db.py до этой правки).
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            scheduled INTEGER NOT NULL DEFAULT 0,
            disable_notification INTEGER NOT NULL DEFAULT 0,
            title_hint TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            run_after REAL,
            lang TEXT NOT NULL DEFAULT 'ru',
            attempts INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    raw.execute(
        "INSERT INTO jobs(url, chat_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (URL, CHAT_ID, time.time(), time.time()),
    )
    raw.commit()
    raw.close()

    # Открытие через Database запускает миграции в __init__.
    db = Database(db_path)

    columns = {row["name"] for row in db.query("PRAGMA table_info(jobs)")}
    assert "quota_user_id" in columns

    # Существующая строка (заведённая до миграции) получает NULL, а не крашит
    # SELECT * — так восстановление её же job'а не падает после апгрейда.
    row = db.query_one("SELECT * FROM jobs WHERE url = ?", (URL,))
    assert row["quota_user_id"] is None


# ── (b) JobStore.add персистит quota_user_id + round trip ─────────────────


def test_job_store_add_persists_quota_user_id(tmp_path):
    job_store = JobStore(Database(tmp_path / "bot.db"))

    db_id = job_store.add(
        URL, CHAT_ID, scheduled=False, disable_notification=False,
        title_hint=None, lang="ru", quota_user_id=EXTERNAL_USER_ID,
    )

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (db_id,))
    assert row["quota_user_id"] == EXTERNAL_USER_ID


def test_job_store_add_defaults_quota_user_id_to_null(tmp_path):
    job_store = JobStore(Database(tmp_path / "bot.db"))

    db_id = job_store.add(
        URL, CHAT_ID, scheduled=False, disable_notification=False,
        title_hint=None, lang="ru",
    )

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (db_id,))
    assert row["quota_user_id"] is None


# ── shared fakes для restore/requeue ───────────────────────────────────────


class _FakeUsers:
    def is_allowed(self, user_id):
        return False


class _FakeServicesForRestore:
    def __init__(self, job_store: JobStore):
        self.job_store = job_store
        self.users = _FakeUsers()
        self.billing = None
        self.summary_queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
        self.summary_queue_lock = asyncio.Lock()
        self.summary_worker_task = None
        self.summary_next_sequence = 0


def _make_job_store(tmp_path) -> JobStore:
    return JobStore(Database(tmp_path / "bot.db"))


# ── (c) restore_pending_jobs переносит quota_user_id через рестарт ────────


async def test_restore_pending_jobs_carries_quota_user_id(tmp_path):
    job_store = _make_job_store(tmp_path)
    db_id = job_store.add(
        URL, CHAT_ID, scheduled=False, disable_notification=False,
        title_hint=None, lang="ru", quota_user_id=EXTERNAL_USER_ID,
    )
    # pending() читает queued/active — строка уже 'queued' после add().
    services = _FakeServicesForRestore(job_store)

    restored = await restore_pending_jobs(services)

    assert restored == 1
    job = services.summary_queue.get_nowait()
    assert job.db_id == db_id
    assert job.quota_user_id == EXTERNAL_USER_ID


# ── (d) _requeue_due_deferred переносит quota_user_id (Q4/Q8/Q12/Q13-ретраи) ──


async def test_requeue_due_deferred_carries_quota_user_id(tmp_path):
    job_store = _make_job_store(tmp_path)
    db_id = job_store.add(
        URL, CHAT_ID, scheduled=False, disable_notification=False,
        title_hint=None, lang="ru", quota_user_id=EXTERNAL_USER_ID,
    )
    job_store.set_deferred(db_id, run_after=1000.0, attempts=1)

    services = _FakeServicesForRestore(job_store)
    assert 1000.0 <= time.time()  # run_after уже в прошлом относительно "сейчас"
    await _requeue_due_deferred(services)

    job = services.summary_queue.get_nowait()
    assert job.db_id == db_id
    assert job.quota_user_id == EXTERNAL_USER_ID
    assert job.transient_retries == 1


# ── (e) enqueue_scheduled_candidate персистит quota_user_id=None ──────────


class _FakeSettingsForScheduled:
    def __init__(self, target_chat_id=CHAT_ID):
        self.monitoring_target_chat_id = target_chat_id


class _FakeWorkerTask:
    """Stands in for an already-running worker task — done() is False so
    enqueue_scheduled_candidate doesn't spawn a real _summary_queue_worker
    (which would need llm/groq fakes to run to completion)."""

    def done(self):
        return False


class _FakeServicesForScheduled:
    def __init__(self, job_store: JobStore):
        self.settings = _FakeSettingsForScheduled()
        self.job_store = job_store
        self.summary_queue: asyncio.PriorityQueue[SummaryJob] = asyncio.PriorityQueue()
        self.summary_queue_lock = asyncio.Lock()
        self.summary_worker_task = _FakeWorkerTask()
        self.summary_next_sequence = 0


def _make_candidate() -> ScheduledCandidate:
    metadata = VideoMetadata(
        video_id="dQw4w9WgXcQ", title="T", channel_name="C", channel_url="https://x",
    )
    feed_entry = FeedEntry(
        video_id="dQw4w9WgXcQ", title="T", url=URL, published_at=None, description="",
    )
    return ScheduledCandidate(
        feed_entry=feed_entry, metadata=metadata, transcript_segments=[],
        transcript_source="none",
    )


async def test_enqueue_scheduled_candidate_persists_null_quota_user_id(tmp_path):
    job_store = _make_job_store(tmp_path)
    services = _FakeServicesForScheduled(job_store)
    channel = MonitoredChannel(channel_id="UC1", channel_url="https://x")

    await enqueue_scheduled_candidate(_make_candidate(), channel, services)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE url = ?", (URL,))
    assert row is not None
    assert row["quota_user_id"] is None

    job = services.summary_queue.get_nowait()
    assert job.quota_user_id is None
