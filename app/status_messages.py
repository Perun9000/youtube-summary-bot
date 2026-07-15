from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Awaitable, TypeVar

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from app.i18n import t
from app.utils import escape_html, extract_video_id

from app.services_container import (
    MAX_TELEGRAM_MESSAGE_CHARS,
    Services,
    SummaryJob,
)


logger = logging.getLogger(__name__)
T = TypeVar("T")


async def _set_service_status(
    services: Services,
    source_message: Message | None,
    text: str,
    job: SummaryJob | None = None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = False,
    bump: bool = False,
) -> Message | None:
    # Resolve the chat_id and the "send" path:
    # - manual jobs: source_message.answer() / .edit_text()
    # - scheduled jobs (no source_message): services.bot.send_message(chat_id=...)
    chat_id: int | None = None
    use_bot_send = False
    if source_message is not None:
        chat_id = source_message.chat.id
    elif (
        job is not None
        and job.scheduled
        and job.chat_id is not None
        and services.bot is not None
    ):
        chat_id = job.chat_id
        use_bot_send = True
    else:
        return None

    rendered_text, effective_parse_mode = await _render_service_status(
        text, services, job, parse_mode
    )
    # If we upgraded to HTML purely because of the hyperlink header, suppress
    # Telegram's link preview so the YouTube card doesn't shadow the status.
    if effective_parse_mode == "HTML" and parse_mode is None:
        disable_web_page_preview = True
    rendered_text = _fit_telegram_message(rendered_text)
    old_message = services.summary_status_messages.get(chat_id)

    services.summary_status_base_texts[chat_id] = text
    services.summary_status_parse_modes[chat_id] = parse_mode
    services.summary_status_disable_previews[chat_id] = disable_web_page_preview

    if old_message and not bump:
        try:
            await old_message.edit_text(
                rendered_text,
                parse_mode=effective_parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return old_message
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return old_message
            logger.warning("status.edit.failed error=%s", exc)

    if old_message:
        await _delete_message_safely(old_message)

    # Scheduled jobs: do not ping the user with each interim status update.
    # Manual jobs keep their existing default-notify behaviour.
    silent = use_bot_send

    if use_bot_send:
        new_message = await services.bot.send_message(
            chat_id=chat_id,
            text=rendered_text,
            parse_mode=effective_parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            disable_notification=silent,
        )
    else:
        new_message = await source_message.answer(
            rendered_text,
            parse_mode=effective_parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
    services.summary_status_messages[chat_id] = new_message
    return new_message
async def _bump_service_status(services: Services, source_message: Message, job: SummaryJob) -> Message:
    chat_id = source_message.chat.id
    text = services.summary_status_base_texts.get(chat_id) or "Генерирую summary..."
    parse_mode = services.summary_status_parse_modes.get(chat_id)
    disable_preview = services.summary_status_disable_previews.get(chat_id, False)
    return await _set_service_status(
        services=services,
        source_message=source_message,
        text=text,
        job=job,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_preview,
        bump=True,
    )
def _forget_service_status(services: Services, chat_id: int) -> None:
    services.summary_status_messages.pop(chat_id, None)
    services.summary_status_base_texts.pop(chat_id, None)
    services.summary_status_parse_modes.pop(chat_id, None)
    services.summary_status_disable_previews.pop(chat_id, None)
async def _delete_service_status(services: Services, chat_id: int) -> None:
    message = services.summary_status_messages.pop(chat_id, None)
    services.summary_status_base_texts.pop(chat_id, None)
    services.summary_status_parse_modes.pop(chat_id, None)
    services.summary_status_disable_previews.pop(chat_id, None)
    if message is not None:
        await _delete_message_safely(message)
async def _render_service_status(
    text: str,
    services: Services,
    job: SummaryJob | None,
    parse_mode: str | None,
) -> tuple[str, str | None]:
    """Compose status message body, injecting a hyperlink header for the active job.

    Returns ``(rendered_text, effective_parse_mode)``. When a job with a known URL
    is supplied, we prepend ``<a href="URL">TITLE</a>`` and force ``parse_mode="HTML"``,
    escaping the plain-text body and queue block so they survive HTML rendering.

    Callers that pass ``parse_mode="HTML"`` are trusted to deliver HTML-safe ``text``
    (e.g. the post-generation info block).
    """
    header = _format_job_header(job)
    queue_block = await _queue_block(services, job)

    if parse_mode == "HTML":
        # Body is already HTML; queue block is plain text and needs escaping.
        queue_block_safe = escape_html(queue_block) if queue_block else ""
        parts = [p for p in (header, text, queue_block_safe) if p]
        return "\n\n".join(parts), "HTML"

    if header:
        # Upgrade to HTML to render the hyperlink. Escape body + queue.
        body = escape_html(text)
        queue_block_safe = escape_html(queue_block) if queue_block else ""
        parts = [p for p in (header, body, queue_block_safe) if p]
        return "\n\n".join(parts), "HTML"

    if queue_block:
        return f"{text}\n\n{queue_block}", parse_mode
    return text, parse_mode
def _format_job_header(job: SummaryJob | None) -> str:
    """Render an HTML hyperlink header for a job, or empty string if URL unknown."""
    if job is None or not job.url:
        return ""
    title_hint = (job.title_hint or "").strip()
    if title_hint:
        label = title_hint
    else:
        try:
            video_id = extract_video_id(job.url)
            label = f"YouTube video {video_id}"
        except Exception:
            label = job.url
    safe_label = escape_html(label)
    safe_url = escape_html(job.url)
    return f'<a href="{safe_url}">{safe_label}</a>'
async def _queue_block(services: Services, job: SummaryJob | None) -> str:
    """Блок «очередь» под статусом. Приватность: получатель видит позицию
    своего ролика и СВОИ ожидающие ролики — чужие заголовки не показываем
    (в PUBLIC_MODE очередь общая на всех пользователей)."""
    if job is None:
        return ""
    async with services.summary_queue_lock:
        active_job = services.summary_active_job
        pending_jobs = list(services.summary_queue._queue)

    queue_all = ([active_job] if active_job is not None else []) + pending_jobs
    total = len(queue_all)
    if total <= 1:
        return ""
    position = next(
        (i for i, queued in enumerate(queue_all, start=1) if queued is job), 1
    )

    lines = [t("status.queue_line", job.lang, position=position, total=total)]
    for queued_job in pending_jobs:
        if queued_job is job or queued_job.chat_id != job.chat_id:
            continue
        lines.append(f"- {_job_label(queued_job)}")
    return "\n".join(lines)
async def _prefetch_job_title(job: SummaryJob, services: Services) -> None:
    try:
        metadata = await asyncio.to_thread(services.youtube.fetch_metadata, job.url)
    except Exception as exc:
        logger.warning("queue.job.title_prefetch_failed sequence=%s url=%s error=%s", job.sequence, job.url, exc)
        return

    title = metadata.title.strip()
    if not title:
        return

    job.title_hint = title
    logger.info("queue.job.title_prefetch_done sequence=%s title=%r", job.sequence, title)
    await _refresh_active_service_status(services)
async def _refresh_active_service_status(services: Services) -> None:
    async with services.summary_queue_lock:
        active_job = services.summary_active_job
    if active_job is None:
        return
    # Both manual and scheduled jobs can refresh: scheduled ones go through
    # services.bot.send_message via _set_service_status with source_message=None.
    if not active_job.scheduled and active_job.message is None:
        return

    chat_id = active_job.chat_id
    text = services.summary_status_base_texts.get(chat_id)
    if not text:
        return

    await _set_service_status(
        services=services,
        source_message=active_job.message,
        text=text,
        job=active_job,
        parse_mode=services.summary_status_parse_modes.get(chat_id),
        disable_web_page_preview=services.summary_status_disable_previews.get(chat_id, False),
    )
def _job_label(job: SummaryJob) -> str:
    if job.title_hint:
        return job.title_hint
    try:
        video_id = extract_video_id(job.url)
    except Exception:
        return job.url
    return f"YouTube video {video_id}"
async def _delete_message_safely(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest as exc:
        logger.warning("status.delete.failed error=%s", exc)
def _fit_telegram_message(text: str) -> str:
    if len(text) <= MAX_TELEGRAM_MESSAGE_CHARS:
        return text
    return f"{text[: MAX_TELEGRAM_MESSAGE_CHARS - 3].rstrip()}..."
async def _run_with_telegram_status(
    *,
    services: Services,
    source_message: Message | None,
    operation: Awaitable[T],
    base_text: str,
    job: SummaryJob,
    interval_sec: int = 30,
    status_getter: Callable[[], str] | None = None,
) -> T:
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            elapsed = int(time.monotonic() - job.enqueued_at)
            status_text = status_getter() if status_getter else ""
            lines = [
                base_text,
                "",
                t("status.elapsed", job.lang, elapsed=_format_elapsed_minutes(elapsed, job.lang)),
            ]
            progress_text = _format_job_progress(job, elapsed)
            if progress_text:
                lines.append(progress_text)
            if status_text:
                lines.extend(["", status_text])
            text = "\n".join(lines)
            await _set_service_status(services, source_message, text, job=job)
            await asyncio.sleep(interval_sec)
        return await task
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise
def _format_elapsed(seconds: int) -> str:
    total_seconds = max(0, seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} {_format_russian_hours(hours)} {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs:02d} сек"
    return f"{secs} сек"
def _format_elapsed_minutes(seconds: int, lang: str = "ru") -> str:
    total_seconds = max(0, seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if lang == "ru":
        if hours:
            return f"{hours} {_format_russian_hours(hours)} {minutes} мин"
        if minutes:
            return f"{minutes} мин"
        return "меньше минуты"
    unit_min = t("status.unit_min", lang)
    unit_h = t("status.unit_h", lang)
    if hours:
        return f"{hours} {unit_h} {minutes} {unit_min}"
    if minutes:
        return f"{minutes} {unit_min}"
    return t("status.elapsed_less_min", lang)
_PROGRESS_BAR_CELLS = 10
_PROGRESS_BAR_FULL = "▰"
_PROGRESS_BAR_EMPTY = "▱"
def _format_job_progress(job: SummaryJob, elapsed_sec: int) -> str:
    if job.progress_estimate_sec is None or job.progress_estimate_sec <= 0:
        return ""

    elapsed = max(0, elapsed_sec)
    est = job.progress_estimate_sec

    # До достижения оценки — линейно 0 → 90%.
    # После оценки — асимптотически ползём от 90 к 95%: бар продолжает
    # заметно двигаться, но не соврёт про «100%» до реального завершения.
    # 95% — потолок, чтобы явно сигналить «пока не готово».
    if elapsed <= est:
        raw_percent = (elapsed / est) * 90.0
    else:
        overrun_ratio = min(1.0, (elapsed - est) / (est * 2))
        raw_percent = 90.0 + 5.0 * overrun_ratio

    percent = int(round(raw_percent))
    if elapsed > 0 and percent == 0:
        percent = 1
    percent = max(0, min(95, percent))

    # Псевдо-progress-bar: 10 ячеек, каждая = 10%. Округляем ВНИЗ, чтобы бар не
    # опережал числовой процент (например, 34% → 3 закрашенных ячейки, не 4).
    filled = min(_PROGRESS_BAR_CELLS, percent // 10)
    empty = _PROGRESS_BAR_CELLS - filled
    bar = _PROGRESS_BAR_FULL * filled + _PROGRESS_BAR_EMPTY * empty
    return f"Прогресс: [{bar}] {percent}%"
def _estimate_job_total_seconds(
    *,
    transcript_chars: int | None,
    chunks_count: int | None,
    transcript_source: str | None,
    llm_provider: str,
) -> float | None:
    if (transcript_chars is None or transcript_chars <= 0) and not chunks_count:
        return None

    chars = max(0, transcript_chars or 0)
    chunks = max(1, chunks_count or 1)

    if llm_provider == "lmstudio":
        # Historical local Qwen/LM Studio runs in bot.log: one 10k chunk often
        # takes 4-8 minutes, and final synthesis adds another several minutes.
        estimate = 240.0 + chunks * 330.0 + max(0, chunks - 1) * 120.0
    else:
        # Calibrated from current OpenRouter logs:
        # - 1 chunk: median full job ~125s historically, ~100s after transcript
        #   publishing was moved in parallel.
        # - 2 chunks: observed full jobs ~245-280s before that parallelization,
        #   so we target ~220s going forward.
        chars_component = min(chars, 120_000) / 6_000
        estimate = (
            45.0
            + chunks * 55.0
            + max(0, chunks - 1) * 45.0
            + chars_component
        )

    if transcript_source == "groq":
        # By the time we can count chunks, Groq transcription has already run,
        # but elapsed is measured from the original link. Add a small cushion so
        # those jobs do not jump too aggressively after re-entering summary.
        estimate += 60.0

    return max(90.0, estimate)
def _format_russian_hours(hours: int) -> str:
    last_two = hours % 100
    last = hours % 10
    if 11 <= last_two <= 14:
        return "часов"
    if last == 1:
        return "час"
    if 2 <= last <= 4:
        return "часа"
    return "часов"
def _format_russian_minutes(minutes: int) -> str:
    last_two = minutes % 100
    last = minutes % 10
    if 11 <= last_two <= 14:
        return "минут"
    if last == 1:
        return "минута"
    if 2 <= last <= 4:
        return "минуты"
    return "минут"
