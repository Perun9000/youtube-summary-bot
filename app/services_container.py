from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re as _re  # локальный alias, чтобы не светить re по всему модулю

from aiogram import Bot
from aiogram.types import Message

from app.billing import BillingStore, QuotaService
from app.config import Settings
from app.channel_posts_store import ChannelPostsStore
from app.db import Database
from app.digest_service import DigestStore
from app.groq_whisper_service import GroqWhisperService
from app.job_store import JobStore
from app.llm_client import LLMClient
from app.morning_digest import MorningDigestStore
from app.summary_cache import SummaryCache
from app.tags_catalog import TagsCatalog
from app.models import TranscriptSegment, VideoMetadata
from app.monitoring_service import MonitoringService, ScanProgress
from app.summarizer import Summarizer
from app.system_prompt_store import SystemPromptStore
from app.telegraph_service import TelegraphService
from app.user_store import UserStore
from app.youtube_service import YouTubeService


MAX_TELEGRAM_MESSAGE_CHARS = 4000
TOP_COMMENT_MAX_CHARS = 2200

# Формат YouTube video_id: ровно 11 символов из base64url-алфавита.
# Используется в /start deep-link'ах от browser-extension'а.
YOUTUBE_VIDEO_ID_RE = _re.compile(r"[A-Za-z0-9_-]{11}")


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
    db_id: int | None = None
    # Transient, не персистится: main worker выставляет "done"/"failed" по
    # результату _process_youtube_job, но маршрут «нет субтитров →
    # transcription_queue» — не финал: job вернётся в summary_queue после
    # Groq. Флаг говорит воркеру не трогать статус на этом проходе (он
    # остаётся "active").
    routed_to_transcription: bool = False
    # Transient, не персистится: pipeline отложил job (премьера ещё не
    # вышла) и уже проставил в БД статус "deferred" + run_after. Воркеру
    # финальный статус трогать не нужно — job поднимет deferred-scheduler.
    deferred_until: float | None = None
    # Квоты внешних пользователей (PUBLIC_MODE). None — безлимит (allowlist,
    # owner, scheduled-мониторинг): ни проверок, ни списаний.
    quota_user_id: int | None = None
    # Вес списания: 1 обычный ролик, 2 — тяжёлый (Groq-транскрипция, ≥1 ч).
    # Выставляется в pipeline, когда выясняется источник транскрипта.
    usage_weight: int = 1
@dataclass
class Services:
    settings: Settings
    users: UserStore
    llm: LLMClient
    youtube: YouTubeService
    summarizer: Summarizer
    telegraph: TelegraphService
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
    digests: "DigestStore | None" = None
    system_prompts: "SystemPromptStore | None" = None
    db: "Database | None" = None
    job_store: "JobStore | None" = None
    morning_digest: "MorningDigestStore | None" = None
    billing: "BillingStore | None" = None
    quota: "QuotaService | None" = None
    # Двухшаговые админ-команды («введи /user_add — бот спросит — ты отвечаешь
    # данными в следующем сообщении»). Ключ — chat_id, значение —
    # PendingAdminInput. Не персистится: после рестарта диалог теряется,
    # пользователь просто начнёт заново.
    pending_admin_inputs: dict[int, "PendingAdminInput"] = field(default_factory=dict)
@dataclass
class PendingAdminInput:
    """Что бот ждёт от owner-а в следующем текстовом сообщении."""

    action: str          # 'user_add' / 'user_remove' / 'prompt_set' / 'cache_drop'
    started_at: float    # time.time(), для тайм-аута
PENDING_ADMIN_TIMEOUT_SEC = 300  # 5 минут — за пределом окна сбрасываем
