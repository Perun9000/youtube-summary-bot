from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, TypeVar

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Settings
from app.llm_client import GenerationUsage, LLMClient
from app.models import Summary, TranscriptSegment, VideoContext, VideoMetadata
from app.monitoring_service import (
    MonitoringService,
    ScheduledCandidate,
    filter_segments_by_spans,
    format_spans_for_humans,
)
from app.qa_service import QAService
from app.summarizer import Summarizer, SummaryProgress
from app.telegraph_service import TelegraphService
from app.transcript_chunker import chunk_transcript, segments_to_text
from app.utils import classify_youtube_url, escape_html, extract_video_id, extract_youtube_url
from app.whisper_service import WhisperService
from app.youtube_service import TranscriptUnavailable, YouTubeService


logger = logging.getLogger(__name__)
T = TypeVar("T")
MAX_TELEGRAM_MESSAGE_CHARS = 4000
TRANSCRIPTS_SUBDIR = "transcripts"


def _save_transcript_to_file(data_dir: Path, video_id: str, transcript_text: str) -> Path:
    transcripts_dir = data_dir / TRANSCRIPTS_SUBDIR
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    path = transcripts_dir / f"{video_id}.txt"
    path.write_text(transcript_text, encoding="utf-8")
    return path


@dataclass
class SummaryJob:
    sequence: int
    message: Message | None
    url: str
    enqueued_at: float
    chat_id: int
    title_hint: str | None = None
    scheduled: bool = False
    disable_notification: bool = False
    pre_fetched_metadata: VideoMetadata | None = None
    pre_fetched_segments: list[TranscriptSegment] | None = None
    pre_fetched_transcript_source: str | None = None
    segment_spans: list[tuple[float, float]] | None = None
    expert_matches: list[str] | None = None
    show_matches: list[str] | None = None
    retry_count: int = 0


@dataclass
class Services:
    settings: Settings
    llm: LLMClient
    youtube: YouTubeService
    whisper: WhisperService
    summarizer: Summarizer
    qa: QAService
    telegraph: TelegraphService
    contexts: dict[int, VideoContext]
    summary_queue: asyncio.Queue[SummaryJob]
    summary_queue_lock: asyncio.Lock
    summary_worker_task: asyncio.Task[None] | None
    summary_active_job: SummaryJob | None
    summary_next_sequence: int
    summary_status_messages: dict[int, Message]
    summary_status_base_texts: dict[int, str]
    summary_status_parse_modes: dict[int, str | None]
    summary_status_disable_previews: dict[int, bool]
    bot: Bot | None = None
    monitoring: MonitoringService | None = None


def build_router(services: Services) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if not _is_allowed(message, services.settings):
            await message.answer("Этот бот закрыт для личного использования.")
            return
        await message.answer(
            "Пришли ссылку на YouTube-ролик. Я верну краткое summary здесь и полный конспект в Telegra.ph."
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        if not _is_allowed(message, services.settings):
            await message.answer("Этот бот закрыт для личного использования.")
            return
        await message.answer(
            "Команды:\n"
            "/reset - забыть текущий ролик\n\n"
            "/models - показать модели, доступные локальному LLM-серверу\n\n"
            "/model - показать модель, которую бот использует для summary и Q&A\n\n"
            "/queue - показать очередь summary\n\n"
            "/stop - остановить текущую генерацию и очистить очередь\n\n"
            "После обработки ролика можно задавать вопросы по нему в этом же чате. "
            "Контекст хранится в памяти контейнера до рестарта."
        )

    @router.message(Command("reset"))
    async def reset(message: Message) -> None:
        if not _is_allowed(message, services.settings):
            await message.answer("Этот бот закрыт для личного использования.")
            return
        services.contexts.pop(message.chat.id, None)
        await message.answer("Текущий ролик забыт.")

    @router.message(Command("models"))
    async def models(message: Message) -> None:
        if not _is_allowed(message, services.settings):
            await message.answer("Этот бот закрыт для личного использования.")
            return

        try:
            models_list = await services.llm.list_models()
        except Exception as exc:
            await message.answer(f"Не удалось получить список моделей из {services.llm.provider_name}: {exc}")
            return

        if not models_list:
            await message.answer(f"{services.llm.provider_name} не вернул доступных моделей.")
            return

        lines = "\n".join(f"- {model}" for model in models_list[:30])
        lines = lines[:3500]
        suffix = "\n\nПоказаны первые 30 моделей." if len(models_list) > 30 else ""
        await message.answer(f"{services.llm.provider_name}: доступные модели\n{lines}{suffix}")

    @router.message(Command("model"))
    async def model(message: Message) -> None:
        if not _is_allowed(message, services.settings):
            await message.answer("Этот бот закрыт для личного использования.")
            return

        try:
            model_name = await services.llm.active_model()
        except Exception as exc:
            await message.answer(f"Не удалось определить активную модель {services.llm.provider_name}: {exc}")
            return

        await message.answer(
            f"Бот использует для summary и Q&A:\n{services.llm.provider_name}: {model_name}"
        )

    @router.message(Command("queue"))
    async def queue(message: Message) -> None:
        if not _is_allowed(message, services.settings):
            await message.answer("Этот бот закрыт для личного использования.")
            return

        await message.answer(await _format_queue_status(services))

    @router.message(Command("stop"))
    async def stop(message: Message) -> None:
        if not _is_allowed(message, services.settings):
            await message.answer("Этот бот закрыт для личного использования.")
            return

        await _stop_summary_queue(message, services)

    @router.message(F.text)
    async def text_message(message: Message) -> None:
        if not _is_allowed(message, services.settings):
            await message.answer("Этот бот закрыт для личного использования.")
            return

        text = message.text or ""
        if text.strip().lower() in {"stop", "стоп"}:
            await _stop_summary_queue(message, services)
            return

        url = extract_youtube_url(text)
        if url:
            kind = classify_youtube_url(url)
            if kind == "channel":
                await _handle_channel_url(message, url, services)
                return
            if kind == "video":
                await _enqueue_summary_job(message, url, services)
                return
            await message.answer(
                "Не разобрал ссылку. Пришли URL ролика или канала YouTube."
            )
            return

        context = services.contexts.get(message.chat.id)
        if not context:
            await message.answer("Сначала пришли ссылку на YouTube-ролик.")
            return

        progress = await message.answer("Думаю над ответом по текущему ролику...")
        try:
            answer = await services.qa.answer(context, text)
            await progress.edit_text(answer[:4000])
        except Exception as exc:
            await progress.edit_text(f"Не удалось ответить на вопрос: {exc}")

    return router


async def _enqueue_summary_job(message: Message, url: str, services: Services) -> None:
    active_job: SummaryJob | None
    async with services.summary_queue_lock:
        services.summary_next_sequence += 1
        sequence = services.summary_next_sequence
        active_job = services.summary_active_job
        active_count = 1 if active_job is not None else 0
        position = active_count + services.summary_queue.qsize() + 1
        job = SummaryJob(
            sequence=sequence,
            message=message,
            url=url,
            enqueued_at=time.monotonic(),
            chat_id=message.chat.id,
        )
        await services.summary_queue.put(job)
        if services.summary_worker_task is None or services.summary_worker_task.done():
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))

        logger.info(
            "queue.job.enqueued sequence=%s chat_id=%s position=%s pending=%s url=%s",
            sequence,
            message.chat.id,
            position,
            services.summary_queue.qsize(),
            url,
        )

    asyncio.create_task(_prefetch_job_title(job, services))

    if position == 1:
        await _set_service_status(
            services=services,
            source_message=message,
            text="Добавил ролик в очередь summary. Начинаю обработку.",
            job=job,
            bump=True,
        )
    elif active_job is not None and active_job.chat_id == message.chat.id:
        await _bump_service_status(services, message, active_job)
    else:
        await _set_service_status(
            services=services,
            source_message=message,
            text=f"Добавил ролик в очередь summary. Позиция: {position}.",
            job=job,
            bump=True,
        )


async def _handle_channel_url(message: Message, url: str, services: Services) -> None:
    if services.monitoring is None:
        await message.answer(
            "Мониторинг каналов выключен. Включи MONITORING_ENABLED=true и перезапусти бота."
        )
        return

    notice = await message.answer("Проверяю канал и добавляю в мониторинг...")
    try:
        channel, added = await services.monitoring.add_channel_by_url(url)
    except Exception as exc:
        logger.exception("monitoring.add_channel.failed url=%s", url)
        await notice.edit_text(f"Не получилось добавить канал. Причина: {exc}")
        return

    label = channel.channel_name or channel.channel_id
    if added:
        text = (
            f"Канал добавлен в мониторинг: {label}.\n"
            f"Новые видео буду проверять раз в сутки."
        )
    else:
        text = f"Канал уже в мониторинге: {label}."
    await notice.edit_text(text)


async def enqueue_scheduled_candidate(
    candidate: ScheduledCandidate, channel, services: Services
) -> None:
    """Enqueue a summary job for a scheduled monitoring hit.

    Called by MonitoringService after the filter pipeline has accepted a video.
    """
    target_chat_id = services.settings.monitoring_target_chat_id
    if target_chat_id is None:
        logger.warning(
            "monitoring.enqueue.no_target_chat video_id=%s channel_id=%s",
            candidate.metadata.video_id,
            channel.channel_id,
        )
        return

    async with services.summary_queue_lock:
        services.summary_next_sequence += 1
        sequence = services.summary_next_sequence
        job = SummaryJob(
            sequence=sequence,
            message=None,
            url=candidate.feed_entry.url,
            enqueued_at=time.monotonic(),
            chat_id=target_chat_id,
            title_hint=candidate.metadata.title or candidate.feed_entry.title,
            scheduled=True,
            disable_notification=True,
            pre_fetched_metadata=candidate.metadata,
            pre_fetched_segments=list(candidate.transcript_segments) or None,
            pre_fetched_transcript_source=candidate.transcript_source,
            segment_spans=list(candidate.segment_spans) or None,
            expert_matches=list(candidate.expert_matches) or None,
            show_matches=list(candidate.show_matches) or None,
        )
        await services.summary_queue.put(job)
        if services.summary_worker_task is None or services.summary_worker_task.done():
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))

    logger.info(
        "monitoring.enqueue.scheduled sequence=%s video_id=%s channel_id=%s experts=%s spans=%s",
        sequence,
        candidate.metadata.video_id,
        channel.channel_id,
        candidate.expert_matches,
        candidate.segment_spans,
    )


async def _is_llm_available(services: Services) -> bool:
    try:
        await asyncio.wait_for(services.llm.list_models(), timeout=10)
        return True
    except Exception as exc:
        logger.info("llm.health.unavailable provider=%s error=%s", services.llm.provider_name, exc)
        return False


async def _summary_queue_worker(services: Services) -> None:
    logger.info("queue.worker.start")
    try:
        while True:
            job: SummaryJob | None = None
            try:
                job = await asyncio.wait_for(services.summary_queue.get(), timeout=1)
            except TimeoutError:
                async with services.summary_queue_lock:
                    if services.summary_queue.empty():
                        services.summary_worker_task = None
                        services.summary_next_sequence = 0
                        logger.info("queue.worker.stop")
                        return
                    continue

            if job.scheduled and not await _is_llm_available(services):
                retry_interval = services.settings.monitoring_llm_retry_interval_sec
                job.retry_count += 1
                logger.info(
                    "queue.job.defer_scheduled sequence=%s chat_id=%s retry=%s sleep_sec=%s",
                    job.sequence,
                    job.chat_id,
                    job.retry_count,
                    retry_interval,
                )
                async with services.summary_queue_lock:
                    services.summary_active_job = None
                await services.summary_queue.put(job)
                services.summary_queue.task_done()
                await asyncio.sleep(max(30, retry_interval))
                continue

            async with services.summary_queue_lock:
                services.summary_active_job = job

            wait_sec = time.monotonic() - job.enqueued_at
            logger.info(
                "queue.job.start sequence=%s chat_id=%s wait_sec=%.1f pending=%s url=%s scheduled=%s",
                job.sequence,
                job.chat_id,
                wait_sec,
                services.summary_queue.qsize(),
                job.url,
                job.scheduled,
            )
            try:
                await _process_youtube_job(job, services)
                logger.info(
                    "queue.job.done sequence=%s chat_id=%s pending=%s",
                    job.sequence,
                    job.chat_id,
                    services.summary_queue.qsize(),
                )
            except asyncio.CancelledError:
                logger.info("queue.job.cancelled sequence=%s chat_id=%s", job.sequence, job.chat_id)
                raise
            except Exception:
                logger.exception("queue.job.failed sequence=%s url=%s", job.sequence, job.url)
            finally:
                async with services.summary_queue_lock:
                    if services.summary_active_job == job:
                        services.summary_active_job = None
                    if services.summary_queue.empty():
                        services.summary_next_sequence = 0
                services.summary_queue.task_done()
    except asyncio.CancelledError:
        logger.info("queue.worker.cancelled")
        async with services.summary_queue_lock:
            if services.summary_worker_task is asyncio.current_task():
                services.summary_worker_task = None
            services.summary_active_job = None
            if services.summary_queue.empty():
                services.summary_next_sequence = 0
    except Exception:
        logger.exception("queue.worker.failed")
        async with services.summary_queue_lock:
            services.summary_worker_task = None
            services.summary_active_job = None
            if services.summary_queue.empty():
                services.summary_next_sequence = 0


async def _stop_summary_queue(message: Message, services: Services) -> None:
    async with services.summary_queue_lock:
        active = services.summary_active_job
        pending_count = _drain_summary_queue(services.summary_queue)
        worker_task = services.summary_worker_task
        services.summary_next_sequence = 0
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()

    if active is None and pending_count == 0:
        await message.answer("Очередь summary уже пуста.")
        return

    if active is not None:
        await message.answer(f"Останавливаю текущую генерацию и очищаю очередь. Удалено из очереди: {pending_count}.")
    else:
        await message.answer(f"Очередь summary очищена. Удалено из очереди: {pending_count}.")


def _drain_summary_queue(queue: asyncio.Queue[SummaryJob]) -> int:
    count = 0
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return count
        queue.task_done()
        count += 1


async def _set_service_status(
    services: Services,
    source_message: Message | None,
    text: str,
    job: SummaryJob | None = None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = False,
    bump: bool = False,
) -> Message | None:
    if source_message is None or (job is not None and job.scheduled):
        # Scheduled jobs run silently — no interim status messages in the chat.
        return None
    chat_id = source_message.chat.id
    rendered_text = await _render_service_status(text, services, job)
    rendered_text = _fit_telegram_message(rendered_text)
    old_message = services.summary_status_messages.get(chat_id)

    services.summary_status_base_texts[chat_id] = text
    services.summary_status_parse_modes[chat_id] = parse_mode
    services.summary_status_disable_previews[chat_id] = disable_web_page_preview

    if old_message and not bump:
        try:
            await old_message.edit_text(
                rendered_text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return old_message
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return old_message
            logger.warning("status.edit.failed error=%s", exc)

    if old_message:
        await _delete_message_safely(old_message)

    new_message = await source_message.answer(
        rendered_text,
        parse_mode=parse_mode,
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


async def _render_service_status(text: str, services: Services, job: SummaryJob | None) -> str:
    queue_block = await _queue_block(services, job)
    if not queue_block:
        return text
    return f"{text}\n\n{queue_block}"


async def _queue_block(services: Services, job: SummaryJob | None) -> str:
    if job is None:
        return ""
    async with services.summary_queue_lock:
        total = services.summary_next_sequence
        pending_jobs = list(services.summary_queue._queue)
    if total <= 1:
        return ""

    lines = [f"очередь: {job.sequence}/{total}"]
    for queued_job in pending_jobs:
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
    # Scheduled jobs run silently — no interim status refresh in chat.
    if active_job.scheduled or active_job.message is None:
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


async def _format_queue_status(services: Services) -> str:
    async with services.summary_queue_lock:
        active = services.summary_active_job
        pending_count = services.summary_queue.qsize()

    if active is None and pending_count == 0:
        return "Очередь summary пуста."

    lines = []
    if active is not None:
        elapsed = int(time.monotonic() - active.enqueued_at)
        lines.append(f"Сейчас обрабатывается: #{active.sequence} ({_format_elapsed(elapsed)} в очереди)")
    else:
        lines.append("Сейчас ничего не обрабатывается.")
    lines.append(f"Ожидают обработки: {pending_count}")
    return "\n".join(lines)


async def _process_youtube_job(job: SummaryJob, services: Services) -> None:
    job_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    message = job.message
    url = job.url
    chat_id = job.chat_id
    video_id = "unknown"
    transcript_source = "unknown"
    title = url
    await _set_service_status(services, message, "Получаю данные ролика...", job=job)
    try:
        video_id = extract_video_id(url)
        logger.info(
            "job.start job_id=%s chat_id=%s video_id=%s url=%s scheduled=%s",
            job_id,
            chat_id,
            video_id,
            url,
            job.scheduled,
        )

        stage_started = time.monotonic()
        if job.pre_fetched_metadata is not None:
            metadata = job.pre_fetched_metadata
            logger.info(
                "job.metadata.reused job_id=%s video_id=%s title=%r",
                job_id,
                metadata.video_id,
                metadata.title,
            )
        else:
            metadata = await asyncio.to_thread(services.youtube.fetch_metadata, url)
            logger.info(
                "job.metadata.done job_id=%s video_id=%s title=%r channel=%r duration_sec=%.1f",
                job_id,
                metadata.video_id,
                metadata.title,
                metadata.channel_name,
                time.monotonic() - stage_started,
            )
        video_id = metadata.video_id
        title = metadata.title

        if job.pre_fetched_segments is not None:
            segments = list(job.pre_fetched_segments)
            transcript_source = job.pre_fetched_transcript_source or "youtube"
            logger.info(
                "job.transcript.reused job_id=%s source=%s segments=%s",
                job_id,
                transcript_source,
                len(segments),
            )
        else:
            try:
                await _set_service_status(services, message, "Пробую получить готовые субтитры YouTube...", job=job)
                stage_started = time.monotonic()
                segments = await asyncio.to_thread(services.youtube.fetch_transcript, video_id)
                transcript_source = "youtube"
                logger.info(
                    "job.transcript.done job_id=%s source=youtube segments=%s duration_sec=%.1f",
                    job_id,
                    len(segments),
                    time.monotonic() - stage_started,
                )
            except TranscriptUnavailable:
                await _set_service_status(
                    services,
                    message,
                    "Субтитры недоступны. Скачиваю аудио и распознаю локально...",
                    job=job,
                )
                logger.info("job.transcript.unavailable job_id=%s fallback=audio", job_id)
                audio_path = await _run_with_telegram_status(
                    services=services,
                    source_message=message,
                    operation=asyncio.to_thread(services.youtube.download_audio, url),
                    base_text="Субтитры недоступны. Скачиваю аудио...",
                    job=job,
                )
                logger.info(
                    "job.audio_download.done job_id=%s path=%s duration_sec=%.1f",
                    job_id,
                    audio_path,
                    time.monotonic() - stage_started,
                )
                stage_started = time.monotonic()
                segments = await _run_with_telegram_status(
                    services=services,
                    source_message=message,
                    operation=asyncio.to_thread(services.whisper.transcribe, audio_path),
                    base_text="Распознаю аудио локально через Whisper...",
                    job=job,
                )
                transcript_source = "whisper"
                logger.info(
                    "job.transcript.done job_id=%s source=whisper segments=%s duration_sec=%.1f",
                    job_id,
                    len(segments),
                    time.monotonic() - stage_started,
                )

        # If this is a scheduled segment-mode job, trim segments to the expert span(s).
        if job.segment_spans:
            original_count = len(segments)
            segments = filter_segments_by_spans(segments, job.segment_spans)
            logger.info(
                "job.segment_filter.done job_id=%s spans=%s before=%s after=%s",
                job_id,
                job.segment_spans,
                original_count,
                len(segments),
            )

        transcript_text = segments_to_text(segments)
        chunks = chunk_transcript(transcript_text, max_chars=services.settings.transcript_chunk_max_chars)
        logger.info(
            "job.chunking.done job_id=%s transcript_chars=%s chunks=%s max_chars=%s",
            job_id,
            len(transcript_text),
            len(chunks),
            services.settings.transcript_chunk_max_chars,
        )

        try:
            saved_path = await asyncio.to_thread(
                _save_transcript_to_file,
                services.settings.bot_data_dir,
                video_id,
                transcript_text,
            )
            logger.info(
                "job.transcript.saved job_id=%s path=%s chars=%s",
                job_id,
                saved_path,
                len(transcript_text),
            )
        except Exception as exc:
            logger.warning("job.transcript.save_failed job_id=%s error=%s", job_id, exc)

        transcript_url: str | None = None
        if segments:
            try:
                await _set_service_status(services, message, "Публикую транскрипт в Telegra.ph...", job=job)
                transcript_url = await _run_with_telegram_status(
                    services=services,
                    source_message=message,
                    operation=services.telegraph.publish_transcript(
                        title=title,
                        video_url=url,
                        video_id=video_id,
                        segments=segments,
                        source=transcript_source,
                    ),
                    base_text="Публикую транскрипт в Telegra.ph...",
                    job=job,
                )
                logger.info("job.transcript.published job_id=%s url=%s", job_id, transcript_url)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job.transcript.publish_failed job_id=%s", job_id)
                transcript_url = None

        usage = GenerationUsage()
        summary_progress = SummaryProgress()
        context_hint = _build_context_hint(job)
        await _set_service_status(services, message, f"Генерирую summary через {services.llm.provider_name}...", job=job)
        summary = await _run_with_telegram_status(
            services=services,
            source_message=message,
            operation=services.summarizer.summarize(
                url=url,
                title=title,
                chunks=chunks,
                progress=summary_progress,
                usage=usage,
                context_hint=context_hint,
            ),
            base_text=f"Генерирую summary через {services.llm.provider_name}...",
            job=job,
            status_getter=summary_progress.status_text,
        )

        await _set_service_status(services, message, "Публикую полный конспект в Telegra.ph...", job=job)
        telegraph_url = await _run_with_telegram_status(
            services=services,
            source_message=message,
            operation=services.telegraph.publish(
                title=title,
                url=url,
                summary=summary,
                transcript_url=transcript_url,
            ),
            base_text="Публикую полный конспект в Telegra.ph...",
            job=job,
        )

        # Scheduled jobs come from daily monitoring, not from an interactive chat
        # session — we don't want to hijack the user's Q&A context with the bot.
        if not job.scheduled:
            services.contexts[chat_id] = VideoContext(
                url=url,
                video_id=video_id,
                title=title,
                transcript_text=transcript_text,
                chunks=chunks,
                summary=summary,
                telegraph_url=telegraph_url,
            )

        total_duration_sec = time.monotonic() - started
        try:
            model_name = await services.llm.active_model()
        except Exception as exc:
            logger.warning("service_info.model_lookup_failed job_id=%s error=%s", job_id, exc)
            model_name = "unknown"
        try:
            loaded_ctx = await services.llm.loaded_context_length()
        except Exception as exc:
            logger.warning("service_info.context_lookup_failed job_id=%s error=%s", job_id, exc)
            loaded_ctx = None

        await _set_service_status(
            services=services,
            source_message=message,
            text=_format_service_info(
                job_id=job_id,
                model=model_name,
                usage=usage,
                transcript_source=transcript_source,
                transcript_chars=len(transcript_text),
                chunks_count=len(chunks),
                total_duration_sec=total_duration_sec,
                settings=services.settings,
                loaded_context_length=loaded_ctx,
            ),
            job=job,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        summary_text = _format_telegram_summary(
            title=title,
            video_url=url,
            summary=summary,
            telegraph_url=telegraph_url,
            channel_name=metadata.channel_name,
            channel_url=metadata.channel_url,
            scheduled=job.scheduled,
            segment_spans=job.segment_spans,
            expert_matches=job.expert_matches,
        )
        await _send_summary_delivery(
            services=services,
            job=job,
            text=summary_text,
        )
        _forget_service_status(services, chat_id)

        logger.info(
            "job.done job_id=%s video_id=%s duration_sec=%.1f telegraph_url=%s "
            "llm_calls=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s llm_sec=%.1f",
            job_id,
            video_id,
            total_duration_sec,
            telegraph_url,
            usage.calls,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.duration_sec,
        )
    except asyncio.CancelledError:
        logger.info("job.cancelled job_id=%s video_id=%s duration_sec=%.1f", job_id, video_id, time.monotonic() - started)
        try:
            await _set_service_status(services, message, "Генерация summary остановлена.", job=job)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("progress.edit.failed error=%s", exc)
        _forget_service_status(services, chat_id)
        raise
    except Exception as exc:
        logger.exception("job.failed job_id=%s video_id=%s duration_sec=%.1f", job_id, video_id, time.monotonic() - started)
        await _set_service_status(services, message, "Генерация summary прервана.", job=job)
        await _send_summary_delivery(
            services=services,
            job=job,
            text=_format_generation_error(video_url=url, title=title, reason=str(exc)),
        )
        _forget_service_status(services, chat_id)
        raise


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
    started = time.monotonic()
    try:
        while not task.done():
            elapsed = int(time.monotonic() - started)
            status_text = status_getter() if status_getter else ""
            lines = [base_text, "", f"Прошло: {_format_elapsed(elapsed)}"]
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


def _format_telegram_summary(
    title: str,
    video_url: str,
    summary: Summary,
    telegraph_url: str,
    channel_name: str,
    channel_url: str,
    scheduled: bool = False,
    segment_spans: list[tuple[float, float]] | None = None,
    expert_matches: list[str] | None = None,
) -> str:
    if channel_name and channel_url:
        channel_line = (
            f'Новое видео на канале <a href="{escape_html(channel_url)}">'
            f"{escape_html(channel_name)}</a>"
        )
    elif channel_name:
        channel_line = f"Новое видео на канале {escape_html(channel_name)}"
    else:
        channel_line = "Новое видео"

    title_line = f'<b><a href="{escape_html(video_url)}">{escape_html(title)}</a></b>'

    segment_line = ""
    if scheduled and segment_spans:
        spans_text = format_spans_for_humans(segment_spans)
        if expert_matches:
            experts_text = ", ".join(expert_matches)
            segment_line = (
                f"<i>Фрагмент с участием: {escape_html(experts_text)} "
                f"({escape_html(spans_text)})</i>"
            )
        else:
            segment_line = f"<i>Фрагмент ролика: {escape_html(spans_text)}</i>"

    overview_line = f"<b>О чем видео:</b>\n{escape_html(summary.overview)}"
    telegraph_line = (
        f'Саммари — <a href="{escape_html(telegraph_url)}">{escape_html(telegraph_url)}</a>'
    )
    reading_line = f"Время чтения: {_estimate_reading_time_minutes(summary)} мин"

    blocks = [channel_line, title_line]
    if segment_line:
        blocks.append(segment_line)
    blocks.extend([overview_line, telegraph_line, reading_line])
    return "\n\n".join(blocks)[:4000]


async def _send_summary_delivery(
    services: Services,
    job: SummaryJob,
    text: str,
) -> None:
    """Send the final summary message to the user.

    Manual jobs reply to the original message. Scheduled jobs go through bot.send_message
    with disable_notification=True so the user isn't pinged overnight.
    """
    if job.message is not None and not job.scheduled:
        await job.message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if services.bot is None:
        logger.warning(
            "delivery.bot_missing sequence=%s chat_id=%s",
            job.sequence,
            job.chat_id,
        )
        return

    await services.bot.send_message(
        chat_id=job.chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        disable_notification=job.disable_notification,
    )


def _build_context_hint(job: SummaryJob) -> str | None:
    """Build a summarizer context hint for segment-mode (scheduled) jobs."""
    if not job.segment_spans:
        return None
    spans_text = format_spans_for_humans(job.segment_spans)
    experts = ", ".join(job.expert_matches) if job.expert_matches else ""
    if experts:
        return (
            f"Это фрагмент длинного шоу с участием: {experts}. "
            f"Таймкоды фрагмента: {spans_text}. "
            "Саммаризируй только этот фрагмент: весь transcript, который ты получаешь, — "
            "это уже вырезанный кусок. Не упоминай остальную часть ролика."
        )
    return (
        f"Это фрагмент длинного ролика (таймкоды: {spans_text}). "
        "Саммаризируй только этот фрагмент, не упоминай остальную часть ролика."
    )


def _format_generation_error(video_url: str, title: str, reason: str) -> str:
    label = title.strip() or video_url
    reason_text = reason.strip() or "неизвестная ошибка"
    return (
        f'генерация саммари для видео <a href="{escape_html(video_url)}">'
        f"{escape_html(label)}</a> прервана.\n\n"
        f"Причина: {escape_html(reason_text)}"
    )[:4000]


def _format_service_info(
    job_id: str,
    model: str,
    usage: GenerationUsage,
    transcript_source: str,
    transcript_chars: int,
    chunks_count: int,
    total_duration_sec: float,
    settings: Settings,
    loaded_context_length: int | None,
) -> str:
    transcript_label = {
        "youtube": "субтитры YouTube",
        "whisper": "локальный Whisper",
        "unknown": "неизвестно",
    }.get(transcript_source, transcript_source)

    tokens_line = (
        f"{usage.prompt_tokens} промпт / {usage.completion_tokens} ответ / {usage.total_tokens} всего"
        if usage.total_tokens
        else "нет данных (LM Studio не вернул usage)"
    )

    configured_ctx = settings.lmstudio_num_ctx
    if loaded_context_length and loaded_context_length != configured_ctx:
        context_line = f"{loaded_context_length} (загружен) / {configured_ctx} (настройка)"
    elif loaded_context_length:
        context_line = str(loaded_context_length)
    else:
        context_line = f"{configured_ctx} (настройка, загруженный неизвестен)"

    lines = [
        f"Модель: {escape_html(model)}",
        f"Контекст: {context_line}",
        f"Температура: {settings.llm_temperature}",
        f"Max tokens ответа: {settings.llm_max_tokens}",
        f"Auto-load модели: {'on' if settings.lmstudio_auto_load else 'off'}",
        f"Размер чанка transcript: {settings.transcript_chunk_max_chars} симв.",
        "",
        f"Токены: {tokens_line}",
        f"Время LLM: {_format_elapsed(int(usage.duration_sec))}",
        f"Источник transcript: {escape_html(transcript_label)}",
        f"Длина transcript: {transcript_chars} симв.",
        f"Чанков: {chunks_count}",
        f"Общее время: {_format_elapsed(int(total_duration_sec))}",
        f"job_id: {escape_html(job_id)}",
    ]

    if transcript_source == "whisper":
        lines.extend(
            [
                "",
                f"Whisper: {escape_html(settings.whisper_model)} "
                f"({escape_html(settings.whisper_device)}, "
                f"{escape_html(settings.whisper_compute_type)})",
            ]
        )

    body = "\n".join(lines)
    return f"<pre>{body}</pre>"[:3900]


def _estimate_reading_time_minutes(summary: Summary) -> int:
    parts: list[str] = [summary.overview]
    parts.extend(summary.key_points)
    for chapter in summary.chapters:
        parts.append(chapter.title)
        parts.append(chapter.notes)
    words = sum(len(part.split()) for part in parts if part)
    return max(1, round(words / 180))


def _is_allowed(message: Message, settings: Settings) -> bool:
    if not settings.allowed_user_ids:
        return True
    return bool(message.from_user and message.from_user.id in settings.allowed_user_ids)
