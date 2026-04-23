from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Awaitable, TypeVar

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Settings
from app.llm_client import GenerationUsage, LLMClient
from app.models import Summary, VideoContext
from app.qa_service import QAService
from app.summarizer import Summarizer, SummaryProgress
from app.telegraph_service import TelegraphService
from app.transcript_chunker import chunk_transcript, segments_to_text
from app.utils import escape_html, extract_video_id, extract_youtube_url
from app.whisper_service import WhisperService
from app.youtube_service import TranscriptUnavailable, YouTubeService


logger = logging.getLogger(__name__)
T = TypeVar("T")
MAX_TELEGRAM_MESSAGE_CHARS = 4000


@dataclass
class SummaryJob:
    sequence: int
    message: Message
    url: str
    enqueued_at: float


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
            await _enqueue_summary_job(message, url, services)
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
        job = SummaryJob(sequence=sequence, message=message, url=url, enqueued_at=time.monotonic())
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

    if position == 1:
        await _set_service_status(
            services=services,
            source_message=message,
            text="Добавил ролик в очередь summary. Начинаю обработку.",
            job=job,
            bump=True,
        )
    elif active_job is not None and active_job.message.chat.id == message.chat.id:
        await _bump_service_status(services, message, active_job)
    else:
        await _set_service_status(
            services=services,
            source_message=message,
            text=f"Добавил ролик в очередь summary. Позиция: {position}.",
            job=job,
            bump=True,
        )


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

            async with services.summary_queue_lock:
                services.summary_active_job = job

            wait_sec = time.monotonic() - job.enqueued_at
            logger.info(
                "queue.job.start sequence=%s chat_id=%s wait_sec=%.1f pending=%s url=%s",
                job.sequence,
                job.message.chat.id,
                wait_sec,
                services.summary_queue.qsize(),
                job.url,
            )
            try:
                await _process_youtube_job(job, services)
                logger.info(
                    "queue.job.done sequence=%s chat_id=%s pending=%s",
                    job.sequence,
                    job.message.chat.id,
                    services.summary_queue.qsize(),
                )
            except asyncio.CancelledError:
                logger.info("queue.job.cancelled sequence=%s chat_id=%s", job.sequence, job.message.chat.id)
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
    source_message: Message,
    text: str,
    job: SummaryJob | None = None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = False,
    bump: bool = False,
) -> Message:
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
    queue_line = await _queue_line(services, job)
    if not queue_line:
        return text
    return f"{text}\n\n{queue_line}"


async def _queue_line(services: Services, job: SummaryJob | None) -> str:
    if job is None:
        return ""
    async with services.summary_queue_lock:
        total = services.summary_next_sequence
    if total <= 1:
        return ""
    return f"очередь: {job.sequence}/{total}"


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
    chat_id = message.chat.id
    video_id = "unknown"
    transcript_source = "unknown"
    title = url
    await _set_service_status(services, message, "Получаю данные ролика...", job=job)
    try:
        video_id = extract_video_id(url)
        logger.info("job.start job_id=%s chat_id=%s video_id=%s url=%s", job_id, chat_id, video_id, url)

        stage_started = time.monotonic()
        metadata = await asyncio.to_thread(services.youtube.fetch_metadata, url)
        video_id = metadata.video_id
        title = metadata.title
        logger.info(
            "job.metadata.done job_id=%s video_id=%s title=%r channel=%r duration_sec=%.1f",
            job_id,
            video_id,
            title,
            metadata.channel_name,
            time.monotonic() - stage_started,
        )

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

        transcript_text = segments_to_text(segments)
        chunks = chunk_transcript(transcript_text, max_chars=services.settings.transcript_chunk_max_chars)
        logger.info(
            "job.chunking.done job_id=%s transcript_chars=%s chunks=%s max_chars=%s",
            job_id,
            len(transcript_text),
            len(chunks),
            services.settings.transcript_chunk_max_chars,
        )

        usage = GenerationUsage()
        summary_progress = SummaryProgress()
        await _set_service_status(services, message, f"Генерирую summary через {services.llm.provider_name}...", job=job)
        summary = await _run_with_telegram_status(
            services=services,
            source_message=message,
            operation=services.summarizer.summarize(
                url=url, title=title, chunks=chunks, progress=summary_progress, usage=usage,
            ),
            base_text=f"Генерирую summary через {services.llm.provider_name}...",
            job=job,
            status_getter=summary_progress.status_text,
        )

        await _set_service_status(services, message, "Публикую полный конспект в Telegra.ph...", job=job)
        telegraph_url = await _run_with_telegram_status(
            services=services,
            source_message=message,
            operation=services.telegraph.publish(title=title, url=url, summary=summary),
            base_text="Публикую полный конспект в Telegra.ph...",
            job=job,
        )

        services.contexts[message.chat.id] = VideoContext(
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

        await message.answer(
            _format_telegram_summary(
                title=title,
                video_url=url,
                summary=summary,
                telegraph_url=telegraph_url,
                channel_name=metadata.channel_name,
                channel_url=metadata.channel_url,
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
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
        await message.answer(
            _format_generation_error(video_url=url, title=title, reason=str(exc)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        _forget_service_status(services, chat_id)
        raise


async def _run_with_telegram_status(
    *,
    services: Services,
    source_message: Message,
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
    minutes, secs = divmod(max(0, seconds), 60)
    if minutes:
        return f"{minutes} мин {secs:02d} сек"
    return f"{secs} сек"


def _format_telegram_summary(
    title: str,
    video_url: str,
    summary: Summary,
    telegraph_url: str,
    channel_name: str,
    channel_url: str,
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
    overview_line = f"<b>О чем видео:</b>\n{escape_html(summary.overview)}"
    telegraph_line = (
        f'Саммари — <a href="{escape_html(telegraph_url)}">{escape_html(telegraph_url)}</a>'
    )
    reading_line = f"Время чтения: {_estimate_reading_time_minutes(summary)} мин"

    return "\n\n".join([channel_line, title_line, overview_line, telegraph_line, reading_line])[:4000]


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
