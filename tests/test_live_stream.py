"""Q12: идущие/только что завершившиеся прямые трансляции — обрабатывать как
премьеры (см. tests/test_premiere.py), а не падать в сырую yt-dlp ошибку
("No video formats found!" — боевой инцидент 2026-08-26, видео HQEiP5W_Wgk,
live_status=is_live).

is_live — эфир идёт, контента ещё нет (yt-dlp вообще не отдаёт форматы).
post_live — эфир кончился, но VOD ещё обрабатывается YouTube (та же
"No video formats found!", просто временное окно короче). Обе ветки
откладывают job через ту же deferred-механику, что премьеры и Q4/Q8-ретраи
(jobs.run_after + status='deferred' + run_deferred_jobs_scheduler), с ОБЩИМ
бюджетом попыток job.transient_retries/MAX_TRANSIENT_RETRIES=3 (не отдельный
лимит — так задано владельцем).

Конвенции фейков — как в tests/test_transient_retry.py.
"""
import asyncio
import time

import pytest

from app.db import Database
from app.job_store import JobStore
from app.models import VideoMetadata
from app.pipeline import (
    LIVE_STREAM_DEFER_DELAY_SEC,
    MAX_TRANSIENT_RETRIES,
    _live_stream_kind,
)
from app.queue_service import _summary_queue_worker
from app.services_container import SummaryJob


OWNER_ID = 555
CHAT_ID = 100
VIDEO_ID = "HQEiP5W_Wgk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def meta(**kw):
    return VideoMetadata(
        video_id=VIDEO_ID, title="Прямой эфир", channel_name="", channel_url="", **kw
    )


# ── _live_stream_kind: чистый классификатор ────────────────────────────────


def test_is_live_is_live_stream_kind():
    assert _live_stream_kind(meta(live_status="is_live")) == "is_live"


def test_post_live_is_live_stream_kind():
    assert _live_stream_kind(meta(live_status="post_live")) == "post_live"


@pytest.mark.parametrize("status", ["", "not_live", "was_live", "is_upcoming"])
def test_other_statuses_are_not_live_stream_kind(status):
    assert _live_stream_kind(meta(live_status=status)) is None


def test_live_stream_delay_map_matches_spec():
    assert LIVE_STREAM_DEFER_DELAY_SEC["is_live"] == 2 * 3600
    assert LIVE_STREAM_DEFER_DELAY_SEC["post_live"] == 30 * 60


# ── shared fakes для интеграционных тестов через _summary_queue_worker ─────


class _FakeSettings:
    def __init__(self, owner_user_id=OWNER_ID):
        self.owner_user_id = owner_user_id


class _FakeUsers:
    def is_owner(self, user_id):
        return user_id == OWNER_ID

    def is_allowed(self, user_id):
        return user_id == OWNER_ID


class _FakeYouTubeMetadata:
    """fetch_metadata всегда отдаёт заданный VideoMetadata (без исключений).
    fetch_transcript по умолчанию падает assertion'ом — live/post_live job
    не должен вообще до него доходить (детект — до транскрипта/квоты)."""

    def __init__(self, metadata: VideoMetadata):
        self._metadata = metadata
        self.calls = 0

    def fetch_metadata(self, url):
        self.calls += 1
        return self._metadata

    def fetch_transcript(self, video_id):
        raise AssertionError(
            "fetch_transcript не должен вызываться для live/post_live job — "
            "детект обязан отсечь его раньше"
        )


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


class _FakeLLM:
    """Succeeds immediately — используется, чтобы job.scheduled=True job'ы
    проходили _is_llm_available() без реального Services.llm (не задействуется
    в наших тестах напрямую, но нужен для совместимости с воркером)."""

    async def list_models(self):
        return []


class _FakeServices:
    def __init__(self, metadata: VideoMetadata, job_store: JobStore):
        self.settings = _FakeSettings()
        self.users = _FakeUsers()
        self.billing = None
        self.quota = None
        self.youtube = _FakeYouTubeMetadata(metadata)
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
    # _summary_queue_worker processes everything currently queued, then stops
    # itself once the queue is empty — no manual task management needed.
    await _summary_queue_worker(services)


# ── (a) is_live → deferred на ~2ч, attempts=1, человеческое сообщение ─────


async def test_is_live_defers_job_instead_of_ytdlp_error(tmp_path):
    job_store = _make_job_store(tmp_path)
    services = _FakeServices(meta(live_status="is_live"), job_store)
    job = _make_job(job_store, transient_retries=0)
    before = time.time()

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 1
    delay = LIVE_STREAM_DEFER_DELAY_SEC["is_live"]
    assert before + delay - 5 <= row["run_after"] <= time.time() + delay + 5

    sent_texts = [call["text"] for call in services.bot.sent]
    assert any("прямая трансляция" in text for text in sent_texts)
    assert any("~2 ч" in text for text in sent_texts)
    assert not any("no video formats" in text.lower() for text in sent_texts)
    assert not any("traceback" in text.lower() for text in sent_texts)


# ── (b) post_live → deferred на ~30 мин ────────────────────────────────────


async def test_post_live_defers_job_with_shorter_delay(tmp_path):
    job_store = _make_job_store(tmp_path)
    services = _FakeServices(meta(live_status="post_live"), job_store)
    job = _make_job(job_store, transient_retries=0)
    before = time.time()

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "deferred"
    assert row["attempts"] == 1
    delay = LIVE_STREAM_DEFER_DELAY_SEC["post_live"]
    assert before + delay - 5 <= row["run_after"] <= time.time() + delay + 5

    sent_texts = [call["text"] for call in services.bot.sent]
    assert any("обрабатывает запись" in text for text in sent_texts)
    assert any("~30 мин" in text for text in sent_texts)


# ── (в) 4-й подъём при live → failed + live.gave_up, без сырой ошибки ─────


async def test_fourth_attempt_still_live_gives_up_with_human_message(tmp_path):
    job_store = _make_job_store(tmp_path)
    services = _FakeServices(meta(live_status="is_live"), job_store)
    assert MAX_TRANSIENT_RETRIES == 3
    # Три деферрала уже были (пришли бы через _requeue_due_deferred, как в
    # tests/test_transient_retry.py) — это четвёртый подряд провал.
    job = _make_job(job_store, transient_retries=MAX_TRANSIENT_RETRIES)

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    assert row["status"] == "failed"

    # Первый send — статус "fetching", второй — финальное сообщение об отказе
    # (тот же паттерн, что test_transient_retry.py::test_third_transient_...).
    assert len(services.bot.sent) == 2
    final_text = services.bot.sent[-1]["text"]
    assert "Пришли ссылку заново" in final_text
    assert "no video formats" not in final_text.lower()
    assert "traceback" not in final_text.lower()
    assert "yt-dlp" not in final_text.lower()


# ── (г) обычный ролик — детект не задет, обработка идёт дальше ────────────


async def test_regular_video_is_not_treated_as_live_stream(tmp_path):
    job_store = _make_job_store(tmp_path)
    services = _FakeServices(meta(live_status=""), job_store)
    job = _make_job(job_store, transient_retries=0)

    class _ReachedTranscriptStage(Exception):
        pass

    def _raise_marker(video_id):
        raise _ReachedTranscriptStage("transcript stage reached")

    services.youtube.fetch_transcript = _raise_marker

    await _run_worker_once(services, job)

    row = job_store._db.query_one("SELECT * FROM jobs WHERE id = ?", (job.db_id,))
    # Не deferred за live/post_live — ролик прошёл детект и упал уже на
    # транскрипте (наш маркер), обычным (нетранзиентным) failed-путём.
    assert row["status"] == "failed"
    assert row["attempts"] == 0
