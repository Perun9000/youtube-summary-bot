"""Таймер «Прошло:» в статус-сообщении — от НАЧАЛА генерации, не от постановки
в очередь (репорт владельца 2026-09-20: ролик ждал в очереди 1ч48м, и статус
показывал «Прошло: 1 час 49 мин» при реальных ~35 минутах работы).

job.started_at проставляется воркером при взятии job'а из очереди (первый раз;
возврат из transcription-очереди точку отсчёта не сбрасывает). Прогресс-бар
тоже считает от started_at — оценка progress_estimate_sec калибрована именно
на время обработки, не на ожидание.
"""

import time

from app.services_container import SummaryJob
from app.status_messages import _job_elapsed_sec

URL = "https://www.youtube.com/watch?v=abc12345678"


def _make_job(**kwargs) -> SummaryJob:
    defaults = dict(
        sequence=1, message=None, url=URL, enqueued_at=time.monotonic(), chat_id=1
    )
    defaults.update(kwargs)
    return SummaryJob(**defaults)


def test_elapsed_counts_from_started_at_when_set():
    now = time.monotonic()
    job = _make_job(enqueued_at=now - 6480.0)  # 1ч48м в очереди
    job.started_at = now - 60.0  # генерация идёт минуту
    assert 55 <= _job_elapsed_sec(job) <= 65


def test_elapsed_falls_back_to_enqueued_at_before_start():
    now = time.monotonic()
    job = _make_job(enqueued_at=now - 120.0)
    assert job.started_at is None
    assert 115 <= _job_elapsed_sec(job) <= 125


def test_started_at_defaults_to_none():
    job = _make_job()
    assert job.started_at is None
