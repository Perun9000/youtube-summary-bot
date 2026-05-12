from __future__ import annotations

import asyncio
import logging
import datetime
import time
import uuid
from collections.abc import Callable
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, TypeVar
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.channel_posts_store import ChannelPost, ChannelPostsStore
from app.groq_whisper_service import GroqWhisperService, GroqWhisperUnavailable
from app import log_analytics
from app.llm_client import GenerationUsage, LLMClient, OpenRouterClient, health_check_with_reason
from app.summary_cache import CachedSummary, SummaryCache
from app.tags_catalog import CANONICAL_FORMATS, TagsCatalog
from app.models import Summary, SummaryTags, TranscriptSegment, VideoComment, VideoContext, VideoMetadata
from app.monitoring_service import (
    MonitoringService,
    ScanProgress,
    ScheduledCandidate,
    filter_segments_by_spans,
    format_spans_for_humans,
)
from app.qa_service import QAService
from app.summarizer import Summarizer, SummaryProgress
from app.telegraph_service import TelegraphService
from app.transcript_chunker import chunk_transcript, segments_to_text
from app.user_store import UserStore
from app.utils import (
    classify_youtube_url,
    escape_html,
    extract_first_url,
    extract_video_id,
    extract_youtube_url,
)
from app.whisper_service import WhisperService
from app.youtube_service import TranscriptUnavailable, YouTubeService


logger = logging.getLogger(__name__)
T = TypeVar("T")
MAX_TELEGRAM_MESSAGE_CHARS = 4000
TOP_COMMENT_MAX_CHARS = 2200
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
    video_duration_sec: float | None = None
    progress_estimate_sec: float | None = None
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
    users: UserStore
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
    monitoring_scan_task: asyncio.Task[None] | None = None
    monitoring_scan_progress: ScanProgress | None = None
    monitoring_scan_started_at: float | None = None
    # Transcription pipeline — отдельная очередь под видео без YouTube-субтитров.
    # Main worker не блокируется ожиданием Groq Whisper'а, продолжает обрабатывать
    # ролики с готовыми субтитрами, пока transcription worker качает аудио и
    # дёргает облачный Whisper параллельно.
    groq_whisper: "GroqWhisperService | None" = None
    transcription_queue: asyncio.Queue[SummaryJob] | None = None
    transcription_queue_lock: asyncio.Lock | None = None
    transcription_worker_task: asyncio.Task[None] | None = None
    transcription_active_job: SummaryJob | None = None
    summary_cache: "SummaryCache | None" = None
    channel_posts: "ChannelPostsStore | None" = None
    tags_catalog: "TagsCatalog | None" = None
    # Двухшаговые админ-команды («введи /user_add — бот спросит — ты отвечаешь
    # данными в следующем сообщении»). Ключ — chat_id, значение —
    # PendingAdminInput. Не персистится: после рестарта диалог теряется,
    # пользователь просто начнёт заново.
    pending_admin_inputs: dict[int, "PendingAdminInput"] = field(default_factory=dict)


@dataclass
class PendingAdminInput:
    """Что бот ждёт от owner-а в следующем текстовом сообщении."""

    action: str          # 'user_add' / 'user_remove'
    started_at: float    # time.time(), для тайм-аута


PENDING_ADMIN_TIMEOUT_SEC = 300  # 5 минут — за пределом окна сбрасываем


def build_router(services: Services) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if not _is_allowed(message, services):
            await message.answer("Этот бот закрыт для личного использования.")
            return
        await message.answer(
            "Пришли ссылку на YouTube-ролик. Я верну краткое summary здесь и полный конспект в Telegra.ph."
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        if not _is_allowed(message, services):
            await message.answer("Этот бот закрыт для личного использования.")
            return
        if not _is_owner(message, services):
            await message.answer(
                "Доступные команды:\n"
                "/start - начать работу\n"
                "/help - помощь\n\n"
                "Пришли ссылку на YouTube-ролик — я верну краткое summary здесь "
                "и полный конспект в Telegra.ph. После обработки можно задавать "
                "вопросы по ролику в этом же чате."
            )
            return
        await message.answer(
            "Команды:\n"
            "/users - список пользователей\n\n"
            "/user_add - добавить пользователя (бот спросит id и имя)\n\n"
            "/user_remove - удалить пользователя (бот спросит id)\n\n"
            "/cancel - отменить начатый диалог (например, /user_add)\n\n"
            "/reset - забыть текущий ролик\n\n"
            "/models - показать модели, доступные локальному LLM-серверу\n\n"
            "/model - показать модель, которую бот использует для summary и Q&A\n\n"
            "/queue - показать очередь summary\n\n"
            "/stop - остановить текущую генерацию и очистить очередь\n\n"
            "/scan_now - вручную запустить сканер мониторинга или показать статус идущего скана\n\n"
            "/scan_stop - прервать запущенный мониторинговый скан\n\n"
            "/llm_mode - показать активный LLM-провайдер и режим (free/paid)\n\n"
            "/llm_paid - переключить OpenRouter между paid и free (тоггл)\n\n"
            "После обработки ролика можно задавать вопросы по нему в этом же чате. "
            "Контекст хранится в памяти контейнера до рестарта."
        )

    @router.message(Command("users"))
    async def users(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        user_lines = []
        for user in services.users.list_users():
            label = f" — {user.name}" if user.name else ""
            marker = " (owner)" if services.users.is_owner(user.user_id) else ""
            user_lines.append(f"- {user.user_id}{label}{marker}")
        users_text = "\n".join(user_lines) if user_lines else "Список пуст."
        await message.answer(
            "Пользователи с доступом:\n"
            f"{users_text}\n\n"
            "Добавить: /user_add 123456789 Имя\n"
            "Удалить: /user_remove 123456789"
        )

    @router.message(Command("user_add"))
    async def user_add(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        # "/user_add 123 Имя" → сразу применяем.
        # "/user_add" без аргументов → запоминаем pending state и просим ввод
        # отдельным сообщением.
        parts = (message.text or "").split(maxsplit=1)
        raw_args = parts[1] if len(parts) > 1 else ""
        if not raw_args.strip():
            services.pending_admin_inputs[message.chat.id] = PendingAdminInput(
                action="user_add", started_at=time.time(),
            )
            await message.answer(
                "Введи Telegram-id и имя одной строкой — например:\n"
                "<code>123456789 Иван</code>\n\n"
                "Или /cancel чтобы отменить.",
                parse_mode="HTML",
            )
            return
        await _apply_user_add(message, raw_args, services)

    @router.message(Command("user_remove"))
    async def user_remove(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        parts = (message.text or "").split(maxsplit=1)
        raw_args = parts[1] if len(parts) > 1 else ""
        if not raw_args.strip():
            services.pending_admin_inputs[message.chat.id] = PendingAdminInput(
                action="user_remove", started_at=time.time(),
            )
            await message.answer(
                "Введи Telegram-id пользователя для удаления — например:\n"
                "<code>123456789</code>\n\n"
                "Или /cancel чтобы отменить.",
                parse_mode="HTML",
            )
            return
        await _apply_user_remove(message, raw_args, services)

    @router.message(Command("cancel"))
    async def cancel(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if services.pending_admin_inputs.pop(message.chat.id, None) is not None:
            await message.answer("Окей, отменил. Никаких действий не сделано.")
        else:
            await message.answer("Сейчас нет активного диалога.")

    @router.message(Command("reset"))
    async def reset(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        services.contexts.pop(message.chat.id, None)
        await message.answer("Текущий ролик забыт.")

    @router.message(Command("models"))
    async def models(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
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
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
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
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        await message.answer(await _format_queue_status(services))

    @router.message(Command("stop"))
    async def stop(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        await _stop_summary_queue(message, services)

    @router.message(Command("scan_now"))
    async def scan_now(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if services.monitoring is None:
            await message.answer(
                "Мониторинг выключен. Включи MONITORING_ENABLED=true и перезапусти бота."
            )
            return

        existing = services.monitoring_scan_task
        if existing is not None and not existing.done():
            await message.answer(_format_scan_status(services))
            return

        asyncio.create_task(_run_manual_scan(message, services))

    @router.message(Command("llm_mode"))
    async def llm_mode(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        await message.answer(_format_llm_mode_status(services))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        """Owner-only: краткий отчёт по логам за последние 30 дней."""
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        # Дёргаем парсинг в отдельном thread'е — чтение 30 файлов не блокирует loop.
        await message.answer("Считаю статистику за 30 дней...")
        try:
            text = await asyncio.to_thread(_compute_stats_for_telegram, services, 30)
        except Exception as exc:
            logger.exception("stats.failed")
            await message.answer(f"Не удалось собрать статистику: {exc}")
            return
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    @router.message(Command("llm_paid"))
    async def llm_paid(message: Message) -> None:
        """Toggle OpenRouter paid mode on/off.

        Off → free-chain (default).
        On → single paid model from OPENROUTER_MODEL_PAID.
        """
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if not isinstance(services.llm, OpenRouterClient):
            await message.answer(
                "Команда работает только при LLM_PROVIDER=openrouter. "
                "Сейчас провайдер: " + services.llm.provider_name
            )
            return

        if services.llm.is_paid_mode():
            # Toggle OFF — back to free chain.
            services.llm.set_paid_mode(False)
            await message.answer(
                "Платный режим выключен. Вернулся на free-цепочку моделей.\n\n"
                + _format_llm_mode_status(services)
            )
            return

        # Toggle ON — switch to paid.
        if not services.llm.has_paid_model():
            await message.answer(
                "OPENROUTER_MODEL_PAID не настроен в .env. Прежде чем включать "
                "платный режим, добавь нужную модель и пересобери бот."
            )
            return
        services.llm.set_paid_mode(True)
        await message.answer(
            "Платный режим включён. Убедись, что на OpenRouter есть баланс.\n\n"
            + _format_llm_mode_status(services)
        )

    @router.message(Command("scan_stop"))
    async def scan_stop(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        existing = services.monitoring_scan_task
        if existing is None or existing.done():
            await message.answer("Сейчас никаких сканов не идёт.")
            return

        existing.cancel()
        await message.answer("Отменяю текущий скан...")

    @router.callback_query(F.data.startswith("download:"))
    async def download_audio_callback(callback: CallbackQuery) -> None:
        """Owner-only кнопка под саммари: скачать аудио YouTube-ролика и
        прислать его прямо в чат как reply к сообщению-саммари."""
        if not services.users.is_owner(callback.from_user.id if callback.from_user else None):
            await callback.answer("Эта кнопка доступна только владельцу.", show_alert=True)
            return
        video_id = (callback.data or "").removeprefix("download:").strip()
        if not video_id:
            await callback.answer("Неверный callback_data — нет video_id.", show_alert=True)
            return
        # Подтверждаем нажатие сразу — иначе у Telegram спиннер крутится 30 сек.
        await callback.answer("Готовлю аудио...")
        asyncio.create_task(_download_audio_to_chat(callback, video_id, services))

    # ─────────────────────── DISABLED: канал-публикация ───────────────────────
    # Фича «опубликовать в канал» временно отключена. Код callback'а и логики
    # публикации сохранён в виде комментариев, чтобы при необходимости можно
    # было быстро вернуть. См. _publish_to_channel ниже + ChannelPostsStore.
    #
    # @router.callback_query(F.data.startswith("publish:"))
    # async def publish_to_channel_callback(callback: CallbackQuery) -> None:
    #     ... (был owner-only с require TELEGRAM_PUBLISH_CHANNEL_ID,
    #     дёргал _publish_to_channel в asyncio.create_task)

    @router.message(F.text)
    async def text_message(message: Message) -> None:
        if not _is_allowed(message, services):
            await message.answer("Этот бот закрыт для личного использования.")
            return

        # Если в этот чат недавно ввели команду без аргументов («/user_add»,
        # «/user_remove»), мы запомнили action — следующий же текст owner'а
        # принимаем как параметры команды.
        pending = services.pending_admin_inputs.get(message.chat.id)
        if pending is not None and _is_owner(message, services):
            if time.time() - pending.started_at > PENDING_ADMIN_TIMEOUT_SEC:
                services.pending_admin_inputs.pop(message.chat.id, None)
                await message.answer(
                    "Прошлый диалог уже устарел (5 мин таймаут). Запусти команду заново."
                )
                return
            services.pending_admin_inputs.pop(message.chat.id, None)
            raw = (message.text or "").strip()
            if pending.action == "user_add":
                await _apply_user_add(message, raw, services)
            elif pending.action == "user_remove":
                await _apply_user_remove(message, raw, services)
            return

        text = message.text or ""
        if text.strip().lower() in {"stop", "стоп"}:
            if not _is_owner(message, services):
                await _answer_owner_only(message, services)
                return
            await _stop_summary_queue(message, services)
            return

        url = extract_youtube_url(text)
        if url:
            kind = classify_youtube_url(url)
            if kind == "channel":
                if not _is_owner(message, services):
                    await message.answer("Пришли ссылку на отдельный YouTube-ролик.")
                    return
                await _handle_channel_url(message, url, services)
                return
            if kind == "video":
                await _enqueue_summary_job(message, url, services)
                return
            await message.answer(
                "Не разобрал ссылку. Пришли URL ролика или канала YouTube."
            )
            return

        # В тексте есть http(s)-URL, но не от YouTube — режем сразу, чтобы не
        # отправлять его в Q&A-ветку (LLM бесполезно скажет «у меня нет доступа
        # к этому ресурсу») и не показывать generic-сообщение «сначала пришли
        # ссылку на ютуб» — пользователь именно ссылку и прислал, просто не ту.
        foreign_url = extract_first_url(text)
        if foreign_url:
            try:
                host = urlparse(foreign_url).netloc or foreign_url
            except Exception:  # noqa: BLE001
                host = foreign_url
            await message.answer(
                f"Это не YouTube-ссылка (<code>{escape_html(host)}</code>). "
                "Я умею только YouTube — пришли URL ролика или канала.",
                parse_mode="HTML",
                disable_web_page_preview=True,
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
    try:
        # Cache hit fast-path: если по этому ролику уже было саммари, отдаём его
        # сразу, не занимая очередь и не дёргая LLM/Whisper.
        cached = _lookup_cached_summary(url, services)
        if cached is not None:
            logger.info(
                "queue.cache.hit chat_id=%s video_id=%s telegraph_url=%s",
                message.chat.id, cached.video_id, cached.telegraph_url,
            )
            await _send_cached_summary_to_chat(message, cached, services)
            return

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
    finally:
        # Удаляем исходное сообщение пользователя с YouTube-ссылкой сразу же,
        # как только ссылка попала в очередь (или была обслужена из кэша).
        # Так превью ссылки в Telegram не дублирует шапку нашей доставки/статуса
        # и чат остаётся чистым на всё время обработки. Бот может удалить
        # user-message в private chat в течение 48 часов; ошибки swallow'им,
        # потому что удаление best-effort и не влияет на саму доставку саммари.
        try:
            await message.delete()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "source_message.delete_failed chat_id=%s error=%s",
                message.chat.id, exc,
            )


SCAN_PROGRESS_THROTTLE_SEC = 1.5


def _compute_stats_for_telegram(services: Services, days: int) -> str:
    """Aggregate logs + render compact HTML for /stats command.

    Synchronous function — caller wraps in ``asyncio.to_thread`` because
    reading + parsing all rotated archives can take a few hundred ms.
    """
    logs_dir = services.settings.bot_data_dir / "logs"
    since = datetime.datetime.now() - datetime.timedelta(days=days)
    events = log_analytics.iter_events(logs_dir, since=since)
    stats = log_analytics.aggregate(events)

    # Резолв chat_id → display name (через UserStore.list_users()).
    # Кэшим один раз перед агрегацией — не бить get'ом по каждому event'у.
    name_by_id: dict[int, str] = {}
    try:
        for u in services.users.list_users():
            if u.name:
                name_by_id[u.user_id] = u.name
    except Exception as exc:  # noqa: BLE001
        logger.warning("stats.user_resolver_failed error=%s", exc)

    def _name_resolver(chat_id: str) -> str | None:
        try:
            return name_by_id.get(int(chat_id))
        except (TypeError, ValueError):
            return None

    return log_analytics.format_telegram(
        stats,
        name_resolver=_name_resolver,
        summary_cache=services.summary_cache,
    )


def _format_llm_mode_status(services: Services) -> str:
    """Render the active LLM provider/mode for /llm_mode."""
    llm = services.llm
    provider = llm.provider_name
    if not isinstance(llm, OpenRouterClient):
        return (
            f"Провайдер: {provider}\n"
            "Переключение режимов /llm_paid /llm_free доступно только для OpenRouter."
        )

    mode = "platный" if llm.is_paid_mode() else "бесплатный (free)"
    chain = llm.current_chain()
    chain_lines = "\n".join(f"  {i+1}. {m}" for i, m in enumerate(chain)) or "  (пусто)"
    snap = llm.budget.snapshot()
    settings = services.settings

    lines = [
        f"Провайдер: {provider}",
        f"Режим: {mode}",
        "",
        "Модели в порядке приоритета:",
        chain_lines,
        "",
    ]

    if llm.is_paid_mode():
        lines.append(
            "Платная модель — без fallback'а. Лимиты:"
        )
        if settings.openrouter_daily_budget_usd > 0:
            lines.append(
                f"  Дневной бюджет: ${snap.get('spent_usd', 0.0):.4f} / "
                f"${settings.openrouter_daily_budget_usd:.2f}"
            )
        else:
            lines.append("  Дневной бюджет: отключён ($0 = без лимита)")
    else:
        passes = settings.openrouter_fallback_retry_passes + 1
        delay = settings.openrouter_fallback_retry_delay_sec
        lines.append(
            f"Fallback: {passes} прохода × {len(chain)} моделей "
            f"(задержка между проходами {delay}с). Лимиты:"
        )

    if settings.openrouter_daily_request_limit > 0:
        lines.append(
            f"  Запросов сегодня: {snap.get('request_count', 0)} / "
            f"{settings.openrouter_daily_request_limit}"
        )
    else:
        lines.append("  Дневной лимит запросов: отключён")

    lines.append("")
    lines.append("Команда переключения: /llm_paid (тоггл paid ↔ free)")
    return "\n".join(lines)


def _format_scan_status(services: Services) -> str:
    """Render a fresh status snapshot for /scan_now invoked while a scan is already running."""
    snap = services.monitoring_scan_progress
    if snap is None:
        # Task created but progress callback hasn't fired yet (very early stage).
        return "Скан запущен, подбираю первый канал..."

    if snap.current_channel is None:
        return (
            f"Скан почти завершён: {snap.channels_done}/{snap.channels_total}. "
            f"В очереди summary: {snap.enqueued_total}."
        )
    label = snap.current_channel.channel_name or snap.current_channel.channel_id
    return (
        f"Скан уже идёт.\n"
        f"Прогресс: {snap.channels_done}/{snap.channels_total}\n"
        f"Сейчас: {label}\n"
        f"В очереди summary: {snap.enqueued_total}\n\n"
        f"Чтобы прервать — /scan_stop."
    )


async def _run_manual_scan(message: Message, services: Services) -> None:
    """Hand-trigger a monitoring scan and report progress to Telegram.

    Fires from /scan_now. The scan itself takes the same lock the daily
    scheduler uses, so a parallel scheduled tick won't double-run.

    Renders progress into a single status message that is edited as each
    channel finishes; edits are throttled so we don't hit Telegram's
    rate limit on per-message edits.

    Cancellation: if the wrapping asyncio.Task is cancelled (e.g. via
    /scan_stop), we replace the status text and bail out. The
    MonitoringService unwinds httpx/lock state on its own.
    """
    if services.monitoring is None:
        return

    rules = services.monitoring.config.rules
    channels_count = len(rules.channels)
    if channels_count == 0:
        await message.answer(
            "В data/monitoring.yaml не задан ни один канал. Добавь хэндлы и повтори."
        )
        return

    notice = await message.answer(
        f"Запускаю ручной скан мониторинга по {channels_count} каналам. "
        f"Это может занять пару минут."
    )

    last_edit_at = 0.0
    last_text = ""

    async def _edit_status(text: str, force: bool = False) -> None:
        nonlocal last_edit_at, last_text
        now = time.monotonic()
        if not force and now - last_edit_at < SCAN_PROGRESS_THROTTLE_SEC:
            return
        if text == last_text:
            return
        try:
            await notice.edit_text(text)
            last_edit_at = now
            last_text = text
        except Exception:
            # Telegram could throw "message is not modified" or rate-limit,
            # both are non-fatal for the scan itself.
            pass

    async def _on_progress(snapshot: ScanProgress) -> None:
        services.monitoring_scan_progress = snapshot
        if snapshot.current_channel is not None:
            current_label = (
                snapshot.current_channel.channel_name
                or snapshot.current_channel.channel_id
            )
            text = (
                f"Сканирую мониторинг: {snapshot.channels_done}/{snapshot.channels_total}\n"
                f"Сейчас: {current_label}\n"
                f"В очереди summary: {snapshot.enqueued_total}"
            )
            await _edit_status(text)

    services.monitoring_scan_task = asyncio.current_task()
    services.monitoring_scan_progress = None
    services.monitoring_scan_started_at = time.monotonic()
    async def _check_llm() -> tuple[bool, str]:
        return await health_check_with_reason(services.llm)

    try:
        try:
            enqueued = await services.monitoring.run_scan(
                progress=_on_progress,
                llm_check=_check_llm,
            )
        except asyncio.CancelledError:
            snap = services.monitoring_scan_progress
            done = snap.channels_done if snap is not None else 0
            enq = snap.enqueued_total if snap is not None else 0
            await _edit_status(
                f"Скан остановлен. Прошёл каналов: {done}/{channels_count}. "
                f"В очередь успели добавить: {enq}.",
                force=True,
            )
            raise
        except Exception as exc:
            logger.exception("monitoring.scan_now.failed")
            await _edit_status(f"Скан упал: {exc}", force=True)
            return

        if enqueued == 0:
            final = (
                f"Скан завершён ({channels_count}/{channels_count}). "
                f"Новых подходящих видео не найдено: либо уже прошли генерацию, "
                f"либо не прошли фильтры."
            )
        else:
            final = (
                f"Скан завершён ({channels_count}/{channels_count}). "
                f"В очередь summary добавлено: {enqueued}. "
                f"Жди уведомлений по мере готовности."
            )
        await _edit_status(final, force=True)
    finally:
        services.monitoring_scan_task = None
        services.monitoring_scan_progress = None
        services.monitoring_scan_started_at = None


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


async def _enqueue_transcription_job(job: SummaryJob, services: Services) -> None:
    """Push a job into the transcription queue and ensure a worker is running.

    Called from the main worker when it learns YouTube has no captions for
    the video. The transcription worker downloads audio, calls Groq Whisper,
    populates ``job.pre_fetched_segments`` and re-enqueues to summary_queue.
    """
    if services.transcription_queue is None or services.transcription_queue_lock is None:
        raise RuntimeError(
            "transcription_queue не инициализирована (см. main.py при старте бота)."
        )
    async with services.transcription_queue_lock:
        await services.transcription_queue.put(job)
        if (
            services.transcription_worker_task is None
            or services.transcription_worker_task.done()
        ):
            services.transcription_worker_task = asyncio.create_task(
                _transcription_queue_worker(services)
            )
        logger.info(
            "transcription_queue.enqueued sequence=%s pending=%s url=%s",
            job.sequence,
            services.transcription_queue.qsize(),
            job.url,
        )


async def _transcription_queue_worker(services: Services) -> None:
    """Background worker that processes the transcription queue.

    Pulls one job at a time:
    1. Downloads audio via yt-dlp.
    2. Calls Groq Whisper (free tier, multipart upload, returns segments).
    3. Stamps the job with ``pre_fetched_segments`` + ``pre_fetched_transcript_source="groq"``.
    4. Pushes the job back to ``summary_queue`` so the main worker continues
       the regular path (no second transcript-fetch attempt — pre_fetched is
       respected).

    Failures are logged + reported to the user; job is dropped (not retried).
    """
    logger.info("transcription_queue.worker.start")
    queue = services.transcription_queue
    if queue is None or services.transcription_queue_lock is None:
        return
    try:
        while True:
            job: SummaryJob | None = None
            try:
                job = await asyncio.wait_for(queue.get(), timeout=1)
            except TimeoutError:
                async with services.transcription_queue_lock:
                    if queue.empty():
                        services.transcription_worker_task = None
                        logger.info("transcription_queue.worker.stop")
                        return
                    continue

            async with services.transcription_queue_lock:
                services.transcription_active_job = job

            logger.info(
                "transcription_queue.job.start sequence=%s url=%s pending=%s",
                job.sequence, job.url, queue.qsize(),
            )
            try:
                await _process_transcription_job(job, services)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "transcription_queue.job.failed sequence=%s url=%s",
                    job.sequence, job.url,
                )
            finally:
                async with services.transcription_queue_lock:
                    if services.transcription_active_job == job:
                        services.transcription_active_job = None
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("transcription_queue.worker.cancelled")
        async with services.transcription_queue_lock:
            if services.transcription_worker_task is asyncio.current_task():
                services.transcription_worker_task = None
            services.transcription_active_job = None
    except Exception:
        logger.exception("transcription_queue.worker.failed")
        async with services.transcription_queue_lock:
            services.transcription_worker_task = None
            services.transcription_active_job = None


async def _process_transcription_job(job: SummaryJob, services: Services) -> None:
    """One transcription cycle: download audio → Groq → re-enqueue."""
    started = time.monotonic()

    # Status reporting: чтобы Telegram-сообщение жило, обновим его на "скачиваю аудио".
    await _set_service_status(
        services, job.message, "Скачиваю аудио для распознавания через Groq...", job=job
    )

    download_started = time.monotonic()
    audio_path = await asyncio.to_thread(services.youtube.download_audio, job.url)
    download_duration = time.monotonic() - download_started
    logger.info(
        "transcription_queue.audio_download.done sequence=%s path=%s duration_sec=%.1f",
        job.sequence, audio_path, download_duration,
    )

    await _set_service_status(
        services, job.message, "Распознаю аудио через Groq Whisper Large v3 Turbo...", job=job
    )

    try:
        segments = await services.groq_whisper.transcribe(Path(audio_path))
    except GroqWhisperUnavailable as exc:
        logger.warning(
            "transcription_queue.groq_unavailable sequence=%s reason=%s",
            job.sequence, exc,
        )
        await _send_transcription_failure(
            services, job,
            f"Groq Whisper недоступен: {exc}",
        )
        _cleanup_audio_file(audio_path)
        return
    except Exception as exc:
        logger.exception("transcription_queue.groq_failed sequence=%s", job.sequence)
        await _send_transcription_failure(
            services, job,
            f"Ошибка распознавания на Groq: {exc}",
        )
        _cleanup_audio_file(audio_path)
        return
    finally:
        # Удаляем исходное аудио — даже если Groq упал, оно нам уже не нужно.
        _cleanup_audio_file(audio_path)

    if not segments:
        logger.warning(
            "transcription_queue.empty_result sequence=%s url=%s",
            job.sequence, job.url,
        )
        await _send_transcription_failure(
            services, job, "Groq вернул пустой транскрипт."
        )
        return

    duration = time.monotonic() - started
    logger.info(
        "transcription_queue.job.done sequence=%s segments=%s duration_sec=%.1f "
        "(download=%.1fs)",
        job.sequence, len(segments), duration, download_duration,
    )

    # Стамп: транскрипт получен, отдаём обратно в summary_queue.
    job.pre_fetched_segments = list(segments)
    job.pre_fetched_transcript_source = "groq"
    # Сбросим pre_fetched_metadata, если он не пришёл — пусть main worker
    # перетянет actual метаданные ролика заново. (Скорее всего тут он None.)

    await _set_service_status(
        services, job.message,
        "Распознавание завершено. Возвращаю в очередь summary...", job=job,
    )

    async with services.summary_queue_lock:
        await services.summary_queue.put(job)
        if services.summary_worker_task is None or services.summary_worker_task.done():
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))


def _cleanup_audio_file(audio_path) -> None:
    try:
        Path(audio_path).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("transcription_queue.audio_cleanup_failed path=%s error=%s", audio_path, exc)


async def _send_transcription_failure(services: Services, job: SummaryJob, reason: str) -> None:
    """Tell the user the job died in transcription; don't re-enqueue."""
    text = (
        f"Не удалось получить транскрипт ролика.\n\n"
        f"Причина: {reason}\n\n"
        f"Ссылка: {job.url}"
    )
    try:
        if job.message is not None and not job.scheduled:
            await job.message.answer(text)
        elif services.bot is not None and job.chat_id:
            await services.bot.send_message(
                chat_id=job.chat_id,
                text=text,
                disable_notification=job.disable_notification,
            )
    except Exception:
        logger.exception(
            "transcription_queue.failure_delivery_failed sequence=%s", job.sequence
        )


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


async def _format_queue_status(services: Services) -> str:
    async with services.summary_queue_lock:
        summary_active = services.summary_active_job
        summary_pending = services.summary_queue.qsize()

    transcription_active = None
    transcription_pending = 0
    if (
        services.transcription_queue is not None
        and services.transcription_queue_lock is not None
    ):
        async with services.transcription_queue_lock:
            transcription_active = services.transcription_active_job
            transcription_pending = services.transcription_queue.qsize()

    nothing_in_summary = summary_active is None and summary_pending == 0
    nothing_in_transcription = transcription_active is None and transcription_pending == 0
    if nothing_in_summary and nothing_in_transcription:
        return "Все очереди пусты."

    lines: list[str] = []
    lines.append("📝 Очередь summary:")
    if summary_active is not None:
        elapsed = int(time.monotonic() - summary_active.enqueued_at)
        lines.append(
            f"  Сейчас: #{summary_active.sequence} ({_format_elapsed(elapsed)} в очереди)"
        )
    else:
        lines.append("  Сейчас: ничего не обрабатывается")
    lines.append(f"  Ожидают: {summary_pending}")

    lines.append("")
    lines.append("🎙 Очередь распознавания (Groq Whisper):")
    if transcription_active is not None:
        elapsed = int(time.monotonic() - transcription_active.enqueued_at)
        lines.append(
            f"  Сейчас: #{transcription_active.sequence} ({_format_elapsed(elapsed)} в очереди)"
        )
    else:
        lines.append("  Сейчас: ничего не распознаётся")
    lines.append(f"  Ожидают: {transcription_pending}")
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
    transcript_publish_task: asyncio.Task[str | None] | None = None

    # Cache check at the very top — covers scheduled jobs + race-conditions
    # where the same video was queued twice in quick succession.
    if _is_job_cacheable(job):
        cached = _lookup_cached_summary(url, services)
        if cached is not None:
            logger.info(
                "job.cache.hit job_id=%s chat_id=%s video_id=%s telegraph_url=%s",
                job_id, chat_id, cached.video_id, cached.telegraph_url,
            )
            await _deliver_cached_summary_for_job(job, services, cached)
            return

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
        job.video_duration_sec = metadata.duration_sec or None

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
                # === Локальный Whisper отключён. Cloud-инференс через Groq
                # идёт в отдельной очереди transcription_queue, чтобы main
                # worker не блокировался ожиданием транскрипции на длинных
                # роликах. Старый код локального Whisper закомментирован
                # ниже — оставляю на случай быстрого отката. ===
                #
                # await _set_service_status(
                #     services, message,
                #     "Субтитры недоступны. Скачиваю аудио и распознаю локально...",
                #     job=job,
                # )
                # audio_path = await _run_with_telegram_status(
                #     services=services, source_message=message,
                #     operation=asyncio.to_thread(services.youtube.download_audio, url),
                #     base_text="Субтитры недоступны. Скачиваю аудио...", job=job,
                # )
                # segments = await _run_with_telegram_status(
                #     services=services, source_message=message,
                #     operation=asyncio.to_thread(services.whisper.transcribe, audio_path),
                #     base_text="Распознаю аудио локально через Whisper...", job=job,
                # )
                # transcript_source = "whisper"

                if services.groq_whisper is None or not services.groq_whisper.enabled:
                    raise RuntimeError(
                        "Субтитры YouTube недоступны для этого ролика, "
                        "а GROQ_API_KEY не настроен — облачное распознавание "
                        "выключено. Добавь ключ Groq в .env и перезапусти бот."
                    )
                logger.info(
                    "job.transcript.unavailable job_id=%s fallback=groq_queue",
                    job_id,
                )
                await _set_service_status(
                    services,
                    message,
                    "Субтитры недоступны. Отправляю в очередь распознавания через Groq Whisper...",
                    job=job,
                )
                await _enqueue_transcription_job(job, services)
                logger.info(
                    "job.routed_to_transcription job_id=%s url=%s sequence=%s",
                    job_id,
                    url,
                    job.sequence,
                )
                # Возвращаемся: main worker возьмёт следующий job из summary_queue,
                # а transcription worker сам перенаправит этот job обратно после
                # успешного распознавания.
                return

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
        active_model = await services.llm.active_model()
        chunk_size = services.settings.effective_chunk_max_chars(active_model=active_model)
        chunks = chunk_transcript(transcript_text, max_chars=chunk_size)
        job.progress_estimate_sec = _estimate_job_total_seconds(
            transcript_chars=len(transcript_text),
            chunks_count=len(chunks),
            transcript_source=transcript_source,
            llm_provider=services.settings.llm_provider,
        )
        logger.info(
            "job.chunking.done job_id=%s transcript_chars=%s chunks=%s max_chars=%s "
            "provider=%s active_model=%s",
            job_id,
            len(transcript_text),
            len(chunks),
            chunk_size,
            services.settings.llm_provider,
            active_model,
        )
        logger.info(
            "job.progress_estimate.done job_id=%s estimate_sec=%.1f",
            job_id,
            job.progress_estimate_sec or 0.0,
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
        transcript_publish_task: asyncio.Task[str | None] | None = None
        if segments:
            transcript_publish_task = asyncio.create_task(
                _publish_transcript_background(
                    services=services,
                    job_id=job_id,
                    title=title,
                    video_url=url,
                    video_id=video_id,
                    segments=list(segments),
                    source=transcript_source,
                )
            )

        # Тянем топ-комментарии параллельно с генерацией саммари. yt-dlp на
        # comments-extractor'е тратит 5–15 секунд, и если запускать после LLM,
        # это всё уходит в общий duration. Параллельный запуск экономит ровно
        # это время — к моменту, когда саммари готов, комменты обычно уже на
        # руках. Failure-mode тот же: ошибка/отключённые → пустой список.
        comments_task = asyncio.create_task(_fetch_top_comments_background(services, url, job_id))

        usage = GenerationUsage()
        summary_progress = SummaryProgress()
        context_hint = _build_context_hint(job)
        topic_hint, speaker_hint, host_hint = _build_tags_hints(services)
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
                topic_hint=topic_hint,
                speaker_hint=speaker_hint,
                host_hint=host_hint,
            ),
            base_text=f"Генерирую summary через {services.llm.provider_name}...",
            job=job,
        )

        # Нормализуем теги через TagsCatalog (fuzzy match на каталог) и
        # добавляем тег канала из metadata. Если каталога нет — оставляем
        # как пришло от LLM. Получаем frozen Summary с готовыми тегами.
        summary = dataclasses.replace(
            summary,
            tags=_resolve_summary_tags(
                raw_tags=summary.tags,
                channel_name=getattr(metadata, "channel_name", "") or "",
                services=services,
            ),
        )

        if not comments_task.done():
            await _set_service_status(
                services, message, "Дожидаюсь топ-комментариев...", job=job
            )
        try:
            top_comments = await comments_task
        except Exception:
            logger.exception("job.comments.await_failed job_id=%s", job_id)
            top_comments = []

        if transcript_publish_task is not None:
            if not transcript_publish_task.done():
                await _set_service_status(
                    services,
                    message,
                    "Дожидаюсь публикации транскрипта в Telegra.ph...",
                    job=job,
                )
            transcript_url = await transcript_publish_task

        await _set_service_status(services, message, "Публикую полный конспект в Telegra.ph...", job=job)
        telegraph_url = await _run_with_telegram_status(
            services=services,
            source_message=message,
            operation=services.telegraph.publish(
                title=title,
                url=url,
                summary=summary,
                transcript_url=transcript_url,
                top_comments=top_comments,
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
        # model_name всё ещё нужен ниже — пишем в кэш (CachedSummary.model) и
        # в строку job.done для аналитики. Сам user-facing «service info»-блок
        # (Модель/Контекст/Температура/Токены/Источник transcript/…) убран —
        # owner получает то же чистое саммари, что и обычные пользователи.
        try:
            model_name = await services.llm.active_model()
        except Exception as exc:
            logger.warning("service_info.model_lookup_failed job_id=%s error=%s", job_id, exc)
            model_name = "unknown"

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
            top_comment=top_comments[0] if top_comments else None,
        )
        await _send_summary_delivery(
            services=services,
            job=job,
            text=summary_text,
            video_id=video_id,
        )
        # Сервисное сообщение со статусом («Получаю данные…», «Генерирую
        # summary…» и т.п.) дослужило — удаляем его, чтобы в чате осталось
        # только финальное саммари.
        await _delete_service_status(services, chat_id)
        # NB: исходное user-message с YouTube-ссылкой удалили ещё на этапе
        # `_enqueue_summary_job` (finally-блок), как только ссылка попала
        # в очередь. Здесь ничего удалять не нужно.

        # Кэшируем результат — но только для full-video. Segment-mode даёт
        # частичное саммари по конкретному эксперту, его нельзя считать
        # «каноном» для этого video_id.
        if _is_job_cacheable(job) and services.summary_cache is not None and video_id != "unknown":
            try:
                _save_summary_to_cache(
                    services=services,
                    video_id=video_id,
                    url=url,
                    title=title,
                    metadata=metadata,
                    summary=summary,
                    telegraph_url=telegraph_url,
                    transcript_url=transcript_url,
                    transcript_source=transcript_source,
                    transcript_chars=len(transcript_text),
                    model=model_name,
                    top_comments=top_comments,
                )
            except Exception:
                logger.exception("job.cache.save_failed job_id=%s video_id=%s", job_id, video_id)

        logger.info(
            "job.done job_id=%s video_id=%s duration_sec=%.1f telegraph_url=%s "
            "model=%s "
            "llm_calls=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s llm_sec=%.1f",
            job_id,
            video_id,
            total_duration_sec,
            telegraph_url,
            model_name,
            usage.calls,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.duration_sec,
        )
    except asyncio.CancelledError:
        if transcript_publish_task is not None and not transcript_publish_task.done():
            await _cancel_task_safely(transcript_publish_task)
        logger.info("job.cancelled job_id=%s video_id=%s duration_sec=%.1f", job_id, video_id, time.monotonic() - started)
        try:
            await _set_service_status(services, message, "Генерация summary остановлена.", job=job)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("progress.edit.failed error=%s", exc)
        _forget_service_status(services, chat_id)
        raise
    except Exception as exc:
        if transcript_publish_task is not None and not transcript_publish_task.done():
            await _cancel_task_safely(transcript_publish_task)
        logger.exception("job.failed job_id=%s video_id=%s duration_sec=%.1f", job_id, video_id, time.monotonic() - started)
        await _set_service_status(services, message, "Генерация summary прервана.", job=job)
        await _send_summary_delivery(
            services=services,
            job=job,
            text=_format_generation_error(video_url=url, title=title, reason=str(exc)),
        )
        _forget_service_status(services, chat_id)
        raise


async def _publish_transcript_background(
    *,
    services: Services,
    job_id: str,
    title: str,
    video_url: str,
    video_id: str,
    segments: list[TranscriptSegment],
    source: str,
) -> str | None:
    try:
        transcript_url = await services.telegraph.publish_transcript(
            title=title,
            video_url=video_url,
            video_id=video_id,
            segments=segments,
            source=source,
        )
        logger.info("job.transcript.published job_id=%s url=%s", job_id, transcript_url)
        return transcript_url
    except asyncio.CancelledError:
        logger.info("job.transcript.publish_cancelled job_id=%s", job_id)
        raise
    except Exception:
        logger.exception("job.transcript.publish_failed job_id=%s", job_id)
        return None


async def _fetch_top_comments_background(
    services: Services,
    url: str,
    job_id: str,
) -> list[VideoComment]:
    """Background-friendly wrapper around YouTubeService.fetch_top_comments.

    Same failure semantics as the inline version: log + return empty list,
    so that parallel summary generation never breaks because of comments.
    """
    try:
        comments = await asyncio.to_thread(services.youtube.fetch_top_comments, url)
        logger.info("job.comments.done job_id=%s count=%s", job_id, len(comments))
        return comments
    except asyncio.CancelledError:
        logger.info("job.comments.cancelled job_id=%s", job_id)
        raise
    except Exception:
        logger.exception("job.comments.failed job_id=%s", job_id)
        return []


async def _cancel_task_safely(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("background_task.cancel_failed")


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
                f"Прошло с момента ссылки: {_format_elapsed_minutes(elapsed)}",
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


def _format_elapsed_minutes(seconds: int) -> str:
    total_seconds = max(0, seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} {_format_russian_hours(hours)} {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"


def _format_job_progress(job: SummaryJob, elapsed_sec: int) -> str:
    if job.progress_estimate_sec is None or job.progress_estimate_sec <= 0:
        return ""

    raw_percent = (max(0, elapsed_sec) / job.progress_estimate_sec) * 100
    rounded = int(round(raw_percent / 10) * 10)
    if elapsed_sec > 0 and rounded == 0:
        rounded = 10
    rounded = max(0, min(90, rounded))
    return f"Прогресс: ~{rounded}% (оценка)"


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
    top_comment: VideoComment | None = None,
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

    tags_line = _format_tags_line(summary.tags)
    if tags_line:
        blocks.append(tags_line)

    if top_comment is not None:
        base_text = "\n\n".join(blocks)
        separator_len = 2 if base_text else 0
        available_chars = MAX_TELEGRAM_MESSAGE_CHARS - len(base_text) - separator_len
        top_comment_line = _format_top_comment_line(top_comment, available_chars)
        if top_comment_line:
            blocks.append(top_comment_line)

    return _fit_telegram_message("\n\n".join(blocks))


def _format_tags_line(tags: SummaryTags) -> str:
    """Render tags as a single line: ``🏷 #тема #Гость #Ведущий #формат #Канал``.

    Порядок логический: тема → гости → ведущие → формат → канал. Пустые поля
    просто пропускаются. Если вообще ни одного тега нет — пустая строка.
    """
    parts: list[str] = []
    if tags.topic:
        parts.append(f"#{tags.topic}")
    for sp in tags.speakers:
        if sp:
            parts.append(f"#{sp}")
    for host in tags.hosts:
        if host:
            parts.append(f"#{host}")
    if tags.format:
        parts.append(f"#{tags.format}")
    if tags.channel:
        parts.append(f"#{tags.channel}")
    if not parts:
        return ""
    # Tags as plain text — Telegram сам делает их кликабельными.
    return "🏷 " + " ".join(parts)


def _build_tags_hints(services: Services) -> tuple[str, str, str]:
    """Build prompt hints for the LLM: existing tags it can reuse.

    Returns ``(topic_hint, speaker_hint, host_hint)``, формат — inline
    предложения, готовые к интерполяции в JSON-schema prompt.
    """
    catalog = services.tags_catalog
    if catalog is None:
        return ("", "", "")
    topics = catalog.all_tags("topic")
    speakers = catalog.all_tags("speaker")
    hosts = catalog.all_tags("host")
    topic_hint = ""
    speaker_hint = ""
    host_hint = ""
    if topics:
        sample = ", ".join(topics[:30])
        topic_hint = f" Уже использованные темы (предпочти их, если подходят): {sample}."
    if speakers:
        sample = ", ".join(speakers[:30])
        speaker_hint = f" Уже использованные фамилии гостей (предпочти их, если подходят): {sample}."
    if hosts:
        sample = ", ".join(hosts[:30])
        host_hint = f" Уже использованные фамилии ведущих (предпочти их, если подходят): {sample}."
    return (topic_hint, speaker_hint, host_hint)


def _resolve_summary_tags(
    *,
    raw_tags: SummaryTags,
    channel_name: str,
    services: Services,
) -> SummaryTags:
    """Take raw LLM tags + channel from metadata, produce canonical SummaryTags."""
    catalog = services.tags_catalog
    if catalog is None:
        # Без каталога просто возвращаем то, что пришло, плюс канал.
        channel_tag = _normalize_channel_simple(channel_name)
        return dataclasses.replace(raw_tags, channel=channel_tag)

    topic = catalog.lookup_or_add("topic", raw_tags.topic) or ""
    canonical_speakers = _canonicalize_names(catalog, "speaker", raw_tags.speakers, limit=3)
    canonical_hosts = _canonicalize_names(catalog, "host", raw_tags.hosts, limit=5)
    fmt = catalog.lookup_or_add("format", raw_tags.format) or ""
    channel = catalog.lookup_or_add("channel", channel_name) or ""

    return SummaryTags(
        topic=topic,
        speakers=tuple(canonical_speakers),
        hosts=tuple(canonical_hosts),
        format=fmt,
        channel=channel,
    )


def _canonicalize_names(
    catalog: TagsCatalog, category: str, raw: tuple[str, ...] | list[str], *, limit: int,
) -> list[str]:
    """Прогнать каждое имя через catalog.lookup_or_add, дропать дубликаты."""
    out: list[str] = []
    seen: set[str] = set()
    for name in list(raw)[:limit]:
        canon = catalog.lookup_or_add(category, name)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def _normalize_channel_simple(channel_name: str) -> str:
    """Fallback нормализация имени канала, если каталога нет."""
    s = (channel_name or "").strip()
    if not s:
        return ""
    s = "_".join(s.split())
    return s[:1].upper() + s[1:]


def _format_top_comment_line(top_comment: VideoComment, available_chars: int) -> str:
    if available_chars <= 0:
        return ""

    likes_label = _format_likes(top_comment.like_count)
    prefix = f"💬 <i>Топ-комментарий ({likes_label}):\n«"
    suffix = "»</i>"
    max_body_chars = min(TOP_COMMENT_MAX_CHARS, max(0, available_chars - len(prefix) - len(suffix)))
    if max_body_chars <= 0:
        return ""

    raw_text = top_comment.text.strip()
    snippet = _fit_escaped_text(raw_text, max_body_chars)
    return f"{prefix}{snippet}{suffix}"


def _fit_escaped_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(escape_html(text)) <= max_chars:
        return escape_html(text)

    ellipsis = "..."
    low = 0
    high = len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + ellipsis
        escaped = escape_html(candidate)
        if len(escaped) <= max_chars:
            best = escaped
            low = mid + 1
        else:
            high = mid - 1
    return best or ellipsis[:max_chars]


def _format_likes(count: int) -> str:
    """Render like count with proper Russian declension: '1 лайк' / '2 лайка' / '5 лайков'.

    Once we cross a thousand, we collapse the number to a compact ``1.2к`` /
    ``12к`` form because (a) it's easier on the eye in chat, and (b) once the
    counter is big the exact number stops being interesting.
    """
    if count >= 1000:
        return f"{_format_compact_count(count)} лайков"
    last_two = count % 100
    last = count % 10
    if 11 <= last_two <= 14:
        word = "лайков"
    elif last == 1:
        word = "лайк"
    elif 2 <= last <= 4:
        word = "лайка"
    else:
        word = "лайков"
    return f"{count} {word}"


def _format_compact_count(count: int) -> str:
    """Compact thousands/millions: 1500 → '1.5к', 12500 → '12к', 1_500_000 → '1.5м'."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        if count < 10_000:
            value = round(count / 1000, 1)
            return f"{value:g}к"  # 1к, 1.2к, 9.9к
        return f"{count // 1000}к"
    value = round(count / 1_000_000, 1)
    return f"{value:g}м"


async def _send_summary_delivery(
    services: Services,
    job: SummaryJob,
    text: str,
    video_id: str | None = None,
) -> None:
    """Send the final summary message to the user.

    Manual jobs reply to the original message. Scheduled jobs go through bot.send_message
    with disable_notification=True so the user isn't pinged overnight.

    If ``video_id`` is provided, recipient is the bot's owner and a publish
    channel is configured, we attach an inline button «Опубликовать в канал».
    Friends/scheduled jobs get the same text without the button.
    """
    reply_markup = _build_publish_button(job, services, video_id)
    if job.message is not None and not job.scheduled:
        await job.message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
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
        reply_markup=reply_markup,
    )


def _build_publish_button(
    job: SummaryJob, services: Services, video_id: str | None
) -> InlineKeyboardMarkup | None:
    """Owner-only «🎧 Скачать аудио». Returns None if the recipient isn't
    the bot's owner or video_id is missing — в этих случаях саммари уходит
    без кнопки.

    (Имя функции по-прежнему ``_build_publish_button`` для обратной совместимости
    с существующими callsite'ами. Когда-нибудь переименуем.)
    """
    if not video_id:
        return None
    if not _job_is_owner(job, services):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🎧 Скачать аудио",
                callback_data=f"download:{video_id}",
            )],
        ]
    )


# Лимиты Telegram + размеры аудио, при которых имеет смысл что-то делать.
TELEGRAM_AUDIO_MAX_BYTES = 49 * 1024 * 1024   # 50 МБ - 1 МБ запас
TELEGRAM_AUDIO_CAPTION_LIMIT = 1024            # api/sendAudio caption limit
CHANNEL_POST_TEXT_BUDGET = 950                 # ~80 chars запас под escapes


async def _download_audio_to_chat(
    callback: CallbackQuery,
    video_id: str,
    services: Services,
) -> None:
    """Скачивает аудио YouTube-ролика и шлёт файл в личку как reply к саммари.

    Алгоритм:
    1. Резолвим URL по video_id (через cached summary; если кэша нет — пытаемся
       восстановить «https://www.youtube.com/watch?v=<id>»).
    2. Качаем mp3 через yt-dlp (32 kbps mono — наш дефолт).
    3. Если файл > 49 МБ, ужимаем дальше через ``_ensure_audio_fits_telegram``.
    4. ``send_audio`` в тот же чат, где висит кнопка, с reply на саммари.
    5. На ошибках — отдельным сообщением сообщаем причину.
    """
    if callback.message is None or services.bot is None:
        return
    chat_id = callback.message.chat.id
    summary_message_id = callback.message.message_id

    cache = services.summary_cache
    cached = cache.get(video_id) if cache is not None else None
    if cached is not None and cached.url:
        url = cached.url
        title = cached.title[:64] if cached.title else "YouTube"
        performer = cached.channel_name[:64] if cached.channel_name else "YouTube"
    else:
        # Fallback: соберём URL из video_id. Качества метаданных не будет, но
        # аудио качается.
        url = f"https://www.youtube.com/watch?v={video_id}"
        title = "YouTube"
        performer = "YouTube"

    progress_msg = await services.bot.send_message(
        chat_id=chat_id,
        text="Скачиваю аудио ролика...",
        reply_to_message_id=summary_message_id,
    )

    audio_path: Path | None = None
    try:
        audio_path = await asyncio.to_thread(services.youtube.download_audio, url)
        audio_path = await _ensure_audio_fits_telegram(audio_path)
    except Exception as exc:
        logger.exception("download_audio.failed video_id=%s", video_id)
        try:
            await progress_msg.edit_text(f"Не удалось скачать аудио: {exc}")
        except Exception:
            pass
        return

    try:
        await services.bot.send_audio(
            chat_id=chat_id,
            audio=FSInputFile(str(audio_path)),
            title=title,
            performer=performer,
            reply_to_message_id=summary_message_id,
            disable_notification=True,
        )
        # Промежуточное «скачиваю...» больше не нужно — удалим, чтобы чат
        # не засорять.
        try:
            await progress_msg.delete()
        except Exception:
            pass
    except Exception as exc:
        logger.exception("download_audio.send_failed video_id=%s", video_id)
        try:
            await progress_msg.edit_text(f"Не удалось отправить аудио: {exc}")
        except Exception:
            pass


"""DISABLED: Channel publishing pipeline. Сохранено как docstring чтобы Python
не исполнял эту ветку, но при необходимости легко вернуть.

async def _publish_to_channel(callback, video_id, services):
    chat_id = callback.message.chat.id if callback.message else None
    if chat_id is None:
        return
    cache = services.summary_cache
    cached = cache.get(video_id) if cache is not None else None
    if cached is None:
        await services.bot.send_message(chat_id=chat_id, text="Не нашёл саммари в кэше.")
        return
    posts = services.channel_posts
    target_channel_id = services.settings.telegram_publish_channel_id
    if posts is None or target_channel_id is None or services.bot is None:
        await services.bot.send_message(chat_id=chat_id, text="Канал не настроен.")
        return
    fresh_comments = await _refresh_cached_comments(cached, services, source_label="channel-publish")
    existing = posts.get(video_id)
    if existing is not None and existing.chat_id == target_channel_id:
        # ... editMessageCaption flow с refresh комментариев
        ...
    # ... новая публикация: download_audio → _ensure_audio_fits_telegram → send_audio
    # ... сохранение ChannelPost в store

def _chat_id_to_link(chat_id):
    s = str(chat_id)
    if s.startswith("-100"):
        return s[4:]"""


# Скрытая копия helper'а, оставлена для смежного использования вне канала.
def _chat_id_to_link(chat_id: int) -> str:  # noqa: F811
    """Convert ``-1001234567890`` channel id to the ``1234567890`` form used in
    ``t.me/c/<id>/<message_id>`` deep links."""
    s = str(chat_id)
    if s.startswith("-100"):
        return s[4:]
    if s.startswith("-"):
        return s[1:]
    return s


async def _ensure_audio_fits_telegram(audio_path: Path) -> Path:
    """If audio file > Telegram limit, recompress aggressively.

    Defaulту download_audio уже даёт 32kbps mono mp3 — это покрывает ролики
    до ~3.5 часов в 50 МБ. Fallback нужен только для марафонов или если
    YouTube/yt-dlp выдал большой исходник по какой-то причине. Дожимаем
    до Whisper-grade 24kbps mono 16kHz (~11 МБ/час, лезет ~4.5 часа).
    Если и это не помогло — поднимаем исключение, caller отправит без аудио.
    """
    try:
        size = audio_path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"Не удалось проверить размер аудио: {exc}") from exc
    if size <= TELEGRAM_AUDIO_MAX_BYTES:
        logger.info(
            "channel.audio.size_ok path=%s size_bytes=%s", audio_path, size,
        )
        return audio_path

    output_path = audio_path.with_suffix(audio_path.suffix + ".tg.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-vn",
        "-ac", "1",                  # mono
        "-ar", "16000",              # 16kHz — для речи достаточно
        "-c:a", "libmp3lame",
        "-b:a", "24k",               # ~11 МБ/час
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace")[:500] if stderr else ""
        raise RuntimeError(f"ffmpeg.compress failed (rc={proc.returncode}): {err}")
    new_size = output_path.stat().st_size
    logger.info(
        "channel.audio.compressed input=%s input_size=%s output=%s output_size=%s",
        audio_path, size, output_path, new_size,
    )
    if new_size > TELEGRAM_AUDIO_MAX_BYTES:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Аудио после компрессии всё равно {new_size / 1024 / 1024:.1f} МБ, "
            f"превышает Telegram-лимит {TELEGRAM_AUDIO_MAX_BYTES / 1024 / 1024:.0f} МБ. "
            "Видео слишком длинное для отправки одним файлом."
        )
    return output_path


"""DISABLED helpers для канал-публикации. Сохранены как docstring чтобы Python
не объявлял функции в глобальном scope, но текст легко вернуть.

def _format_channel_post_caption(cached, fresh_comments):
    # builds <=1024 char HTML caption: channel/title/overview/telegraph/tags/top-comment
    ...

def _channel_top_comment_line(c):
    # one-line: 💬 «text»... — N лайков
    ...

def _truncate_plain(text, max_chars):
    # plain-text трим с многоточием
    ...
"""


def _is_job_cacheable(job: SummaryJob) -> bool:
    """We cache only canonical full-video summaries.

    Segment-mode (scheduled jobs that hit `expert_segment_threshold_sec` and
    produced span-filtered transcripts) gets a summary specific to one
    expert window — it would be wrong to serve that as the canonical answer
    when a later user requests the *whole* video.
    """
    return not (job.segment_spans and len(job.segment_spans) > 0)


def _lookup_cached_summary(url: str, services: Services) -> CachedSummary | None:
    """Resolve URL → video_id → cached entry, swallowing parse errors."""
    if services.summary_cache is None:
        return None
    try:
        video_id = extract_video_id(url)
    except Exception:
        return None
    return services.summary_cache.get(video_id)


def _format_cached_summary_text(
    cached: CachedSummary,
    override_top_comments: list[VideoComment] | None = None,
) -> str:
    """Render the cached summary identically to a fresh delivery — no header
    or "this is cached" marker. From the user's perspective, sending a link a
    second time should feel like a normal, fast response. Cache-hits are still
    visible in logs (``queue.cache.hit`` / ``job.cache.hit``) for diagnostics.

    ``override_top_comments`` лет сделать так: «выдать саммари с комментами,
    обновлёнными прямо сейчас», не меняя сам ``cached`` объект.
    """
    summary = cached.to_summary()
    if override_top_comments is not None:
        comments = override_top_comments
    else:
        comments = cached.to_top_comments()
    return _format_telegram_summary(
        title=cached.title,
        video_url=cached.url,
        summary=summary,
        telegraph_url=cached.telegraph_url,
        channel_name=cached.channel_name,
        channel_url=cached.channel_url,
        top_comment=comments[0] if comments else None,
    )


async def _refresh_cached_comments(
    cached: CachedSummary, services: Services, source_label: str
) -> list[VideoComment]:
    """Get fresh top-comments and, if they changed since the cache was made,
    rewrite the cached entry + edit the existing Telegra.ph page so all
    surfaces (Telegram, Telegraph) stay in sync.

    Returns the list of comments to actually display. Falls back to the cached
    ones on any failure — comments-refresh is opportunistic, not critical.
    """
    try:
        fresh = await asyncio.to_thread(services.youtube.fetch_top_comments, cached.url)
    except Exception:
        logger.exception(
            "cache.refresh_comments.failed source=%s video_id=%s",
            source_label, cached.video_id,
        )
        return cached.to_top_comments()

    cached_comments = cached.to_top_comments()
    if _comments_equivalent(fresh, cached_comments):
        logger.info(
            "cache.refresh_comments.unchanged source=%s video_id=%s count=%s",
            source_label, cached.video_id, len(fresh),
        )
        return cached_comments

    logger.info(
        "cache.refresh_comments.changed source=%s video_id=%s old=%s new=%s",
        source_label, cached.video_id, len(cached_comments), len(fresh),
    )

    # Edit Telegra.ph first. If it succeeds — also update the cache so future
    # cache hits stay consistent with the live page. If it fails (most common
    # cause: ``editPage`` only works with the same access_token that originally
    # created the page; pages from previous bot incarnations are read-only),
    # we still hand the *fresh* comments to the Telegram delivery — that's the
    # surface the user actually sees right now. Cache stays untouched in that
    # case so we don't desync the cache from the read-only Telegraph page.
    telegraph_updated = False
    try:
        await services.telegraph.edit(
            cached.telegraph_url,
            title=cached.title,
            video_url=cached.url,
            summary=cached.to_summary(),
            transcript_url=cached.transcript_url,
            top_comments=fresh,
        )
        telegraph_updated = True
    except Exception:
        logger.exception(
            "cache.refresh_comments.telegraph_edit_failed video_id=%s",
            cached.video_id,
        )

    if telegraph_updated and services.summary_cache is not None:
        cached.top_comments = [
            {
                "text": c.text,
                "author": c.author,
                "like_count": c.like_count,
                "is_pinned": c.is_pinned,
            }
            for c in fresh
        ]
        try:
            services.summary_cache.put(cached)
        except Exception:
            logger.exception(
                "cache.refresh_comments.cache_put_failed video_id=%s",
                cached.video_id,
            )

    # Always return the fresh comments — even if Telegraph couldn't be
    # updated, the user gets actual top-comment in their Telegram message.
    return fresh


def _comments_equivalent(a: list[VideoComment], b: list[VideoComment]) -> bool:
    """True if two top-comment lists describe the same audience reaction.

    Equality based on author + text identity (those don't change). Like counts
    drift constantly; we accept ±10 wobble before deciding the page needs a
    rewrite.
    """
    if len(a) != len(b):
        return False
    for ca, cb in zip(a, b):
        if ca.author != cb.author or ca.text != cb.text:
            return False
        if abs(ca.like_count - cb.like_count) > 10:
            return False
    return True


async def _send_cached_summary_to_chat(
    message: Message,
    cached: CachedSummary,
    services: Services,
) -> None:
    """Manual flow: respond to a user message with cached summary + restore Q&A."""
    fresh_comments = await _refresh_cached_comments(cached, services, source_label="manual")
    text = _format_cached_summary_text(cached, override_top_comments=fresh_comments)
    # Только owner получает кнопку «Скачать аудио» (как и при свежем саммари).
    reply_markup: InlineKeyboardMarkup | None = None
    user_id = _message_user_id(message)
    if user_id is not None and services.users.is_owner(user_id):
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="🎧 Скачать аудио",
                    callback_data=f"download:{cached.video_id}",
                )
            ]]
        )
    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )
    _restore_qa_context_from_cache(message.chat.id, cached, services)
    # NB: исходное user-message с YouTube-ссылкой удаляется централизованно
    # в `_enqueue_summary_job` (finally-блок), как только ссылка попала в
    # обработку. Здесь дублировать delete не нужно.


async def _deliver_cached_summary_for_job(
    job: SummaryJob,
    services: Services,
    cached: CachedSummary,
) -> None:
    """Job-level delivery: works for both manual (job.message != None) and
    scheduled (services.bot.send_message). Restores Q&A context only for
    interactive (manual) sessions."""
    fresh_comments = await _refresh_cached_comments(cached, services, source_label="job")
    text = _format_cached_summary_text(cached, override_top_comments=fresh_comments)
    await _send_summary_delivery(
        services=services, job=job, text=text, video_id=cached.video_id
    )
    if not job.scheduled and job.chat_id:
        _restore_qa_context_from_cache(job.chat_id, cached, services)
    # NB: исходное user-message с ссылкой уже удалено в `_enqueue_summary_job`
    # к моменту, когда job попал в обработку. У scheduled-job его и не было.


def _restore_qa_context_from_cache(
    chat_id: int,
    cached: CachedSummary,
    services: Services,
) -> None:
    """Re-hydrate VideoContext for follow-up Q&A when we serve a cached summary.

    We pull the saved transcript text from disk (data/transcripts/<video_id>.txt
    written when the summary was first generated). If the transcript file is
    missing, Q&A still works on summary-only context — just less rich.
    """
    transcript_text = ""
    chunks: list[str] = []
    transcript_path = (
        services.settings.bot_data_dir / TRANSCRIPTS_SUBDIR / f"{cached.video_id}.txt"
    )
    if transcript_path.exists():
        try:
            transcript_text = transcript_path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cache.restore.transcript_read_failed video_id=%s error=%s",
                cached.video_id, exc,
            )
    if transcript_text:
        try:
            active_model = services.settings.openrouter_model_paid  # safe default
            try:
                # Try active model first when available — same chunk size that
                # would be used for fresh generation.
                pass
            except Exception:
                pass
            chunk_size = services.settings.effective_chunk_max_chars(
                active_model=cached.model
            )
            chunks = chunk_transcript(transcript_text, max_chars=chunk_size)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cache.restore.chunking_failed video_id=%s error=%s",
                cached.video_id, exc,
            )

    services.contexts[chat_id] = VideoContext(
        url=cached.url,
        video_id=cached.video_id,
        title=cached.title,
        transcript_text=transcript_text,
        chunks=chunks,
        summary=cached.to_summary(),
        telegraph_url=cached.telegraph_url,
    )


def _save_summary_to_cache(
    *,
    services: Services,
    video_id: str,
    url: str,
    title: str,
    metadata,
    summary,
    telegraph_url: str,
    transcript_url: str | None,
    transcript_source: str,
    transcript_chars: int,
    model: str,
    top_comments: list[VideoComment] | None = None,
) -> None:
    """Persist a freshly-generated summary so future requests for the same
    video_id are answered from cache."""
    if services.summary_cache is None:
        return
    now = time.time()
    iso_time = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat()
    entry = CachedSummary(
        video_id=video_id,
        url=url,
        title=title,
        channel_name=getattr(metadata, "channel_name", "") or "",
        channel_url=getattr(metadata, "channel_url", "") or "",
        summary_overview=summary.overview,
        summary_key_points=list(summary.key_points),
        summary_chapters=[
            {"start": ch.start, "title": ch.title, "notes": ch.notes}
            for ch in summary.chapters
        ],
        summary_raw_text=summary.raw_text,
        telegraph_url=telegraph_url,
        transcript_url=transcript_url,
        transcript_source=transcript_source,
        model=model or "unknown",
        created_at_iso=iso_time,
        created_at_unix=now,
        transcript_chars=transcript_chars,
        top_comments=[
            {
                "text": c.text,
                "author": c.author,
                "like_count": c.like_count,
                "is_pinned": c.is_pinned,
            }
            for c in (top_comments or [])
        ],
        tag_topic=summary.tags.topic,
        tag_speakers=list(summary.tags.speakers),
        tag_hosts=list(summary.tags.hosts),
        tag_format=summary.tags.format,
        tag_channel=summary.tags.channel,
    )
    services.summary_cache.put(entry)


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


def _estimate_reading_time_minutes(summary: Summary) -> int:
    parts: list[str] = [summary.overview]
    parts.extend(summary.key_points)
    for chapter in summary.chapters:
        parts.append(chapter.title)
        parts.append(chapter.notes)
    words = sum(len(part.split()) for part in parts if part)
    return max(1, round(words / 180))


async def _answer_owner_only(message: Message, services: Services) -> None:
    if not _is_allowed(message, services):
        await message.answer("Этот бот закрыт для личного использования.")
        return
    await message.answer("Эта команда доступна только владельцу бота.")


async def _apply_user_add(message: Message, raw_args: str, services: Services) -> None:
    """Parse a "<user_id> [name...]" string and add user. Used both inline
    (``/user_add 123 Иван``) and via two-step pending dialog."""
    parts = raw_args.strip().split(maxsplit=1)
    if not parts:
        await message.answer("Не понял ввод. Нужен Telegram-id и имя.")
        return
    try:
        user_id = int(parts[0])
    except ValueError:
        await message.answer("Telegram user id должен быть числом.")
        return

    name = parts[1] if len(parts) > 1 else ""
    added = services.users.add_user(user_id, name)
    if added:
        await message.answer(f"Пользователь {user_id} добавлен.")
    else:
        await message.answer(f"Пользователь {user_id} уже был в списке. Данные обновлены.")


async def _apply_user_remove(message: Message, raw_args: str, services: Services) -> None:
    """Parse a single user_id and remove that user. Used inline + pending dialog."""
    cleaned = raw_args.strip().split()
    if not cleaned:
        await message.answer("Не понял ввод. Нужен Telegram-id.")
        return
    try:
        user_id = int(cleaned[0])
    except ValueError:
        await message.answer("Telegram user id должен быть числом.")
        return

    try:
        removed = services.users.remove_user(user_id)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    if removed:
        await message.answer(f"Пользователь {user_id} удалён.")
    else:
        await message.answer(f"Пользователя {user_id} нет в списке.")


def _is_allowed(message: Message, services: Services) -> bool:
    return services.users.is_allowed(_message_user_id(message))


def _is_owner(message: Message, services: Services) -> bool:
    return services.users.is_owner(_message_user_id(message))


def _job_is_owner(job: SummaryJob, services: Services) -> bool:
    if job.message is not None and job.message.from_user is not None:
        return services.users.is_owner(job.message.from_user.id)
    return services.users.is_owner(job.chat_id)


def _message_user_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None
