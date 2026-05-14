from __future__ import annotations

import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

from app.bot_handlers import Services, build_router, enqueue_scheduled_candidate
from app.config import Settings, load_settings
from app.digest_service import DigestStore
from app.groq_whisper_service import GroqWhisperService
from app.llm_client import create_llm_client, health_check_with_reason
from app.channel_posts_store import ChannelPostsStore
from app.summary_cache import SummaryCache
from app.tags_catalog import TagsCatalog
from app.monitoring_config import MonitoringConfig
from app.monitoring_service import MonitoringService
from app.monitoring_state import MonitoringState
from app.qa_service import QAService
from app.scheduler_service import run_monitoring_scheduler
from app.summarizer import Summarizer
from app.telegraph_service import TelegraphService
from app.user_store import UserStore
from app.whisper_service import WhisperService
from app.youtube_service import YouTubeService


logger = logging.getLogger(__name__)


PUBLIC_BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Начать работу"),
    BotCommand(command="help", description="Что умеет бот"),
]

OWNER_BOT_COMMANDS: list[BotCommand] = [
    *PUBLIC_BOT_COMMANDS,
    BotCommand(command="users", description="Список пользователей"),
    BotCommand(command="user_add", description="Добавить пользователя"),
    BotCommand(command="user_remove", description="Удалить пользователя"),
    BotCommand(command="reset", description="Забыть текущий ролик"),
    BotCommand(command="models", description="Доступные LLM-модели"),
    BotCommand(command="model", description="Текущая модель бота"),
    BotCommand(command="queue", description="Очередь summary"),
    BotCommand(command="stop", description="Остановить генерацию"),
    BotCommand(command="scan_now", description="Запустить мониторинг сейчас"),
    BotCommand(command="scan_stop", description="Прервать мониторинговый скан"),
    BotCommand(command="llm_mode", description="LLM-провайдер: статус"),
    BotCommand(command="llm_paid", description="LLM: тоггл paid/free"),
    BotCommand(command="stats", description="Статистика бота за 30 дней"),
]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_FILE_NAME = "bot.log"
# Логи ротируются ежедневно (в полночь), хранится до 30 архивов —
# в сумме ~30 дней истории. Достаточно для месячной аналитики через /stats
# и команду scripts/analytics.py.
LOG_FILE_ROTATION_DAYS = 1
LOG_FILE_BACKUP_COUNT = 30


async def configure_bot_commands(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(PUBLIC_BOT_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(PUBLIC_BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    if settings.owner_user_id is not None:
        await bot.set_my_commands(
            OWNER_BOT_COMMANDS,
            scope=BotCommandScopeChat(chat_id=settings.owner_user_id),
        )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def configure_logging(data_dir: Path) -> None:
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = TimedRotatingFileHandler(
        logs_dir / LOG_FILE_NAME,
        when="midnight",
        interval=LOG_FILE_ROTATION_DAYS,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


async def main() -> None:
    settings = load_settings()
    settings.bot_data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(settings.bot_data_dir)

    llm = create_llm_client(settings)
    bot = Bot(token=settings.telegram_bot_token)
    groq_whisper = GroqWhisperService(settings)
    if groq_whisper.enabled:
        logger.info(
            "groq.boot enabled=true model=%s base_url=%s",
            settings.groq_whisper_model, settings.groq_base_url,
        )
    else:
        logger.info("groq.boot enabled=false reason=no_api_key")

    summary_cache = SummaryCache(
        settings.summary_cache_path,
        ttl_days=settings.summary_cache_ttl_days,
    )
    logger.info(
        "summary_cache.boot path=%s entries=%s ttl_days=%s",
        settings.summary_cache_path,
        summary_cache.size(),
        settings.summary_cache_ttl_days,
    )

    channel_posts = ChannelPostsStore(settings.channel_posts_path)
    logger.info(
        "channel_posts.boot path=%s entries=%s publish_channel_id=%s",
        settings.channel_posts_path,
        channel_posts.size(),
        settings.telegram_publish_channel_id,
    )

    tags_catalog = TagsCatalog(settings.tags_catalog_path)
    logger.info(
        "tags_catalog.boot path=%s topic=%s speaker=%s format=%s channel=%s",
        settings.tags_catalog_path,
        len(tags_catalog.all_tags("topic")),
        len(tags_catalog.all_tags("speaker")),
        len(tags_catalog.all_tags("format")),
        len(tags_catalog.all_tags("channel")),
    )

    digest_store = DigestStore(
        digests_path=settings.digests_path,
        pins_path=settings.digest_pins_path,
    )
    logger.info(
        "digests.boot digests_path=%s pins_path=%s",
        settings.digests_path, settings.digest_pins_path,
    )
    user_store = UserStore(
        settings.allowed_users_path,
        seed_user_ids=settings.allowed_user_ids,
        owner_user_id=settings.owner_user_id,
    )
    logger.info(
        "users.boot path=%s count=%s owner_user_id=%s",
        settings.allowed_users_path,
        len(user_store.list_users()),
        settings.owner_user_id,
    )

    services = Services(
        settings=settings,
        users=user_store,
        llm=llm,
        youtube=YouTubeService(settings),
        whisper=WhisperService(settings),
        summarizer=Summarizer(
            llm,
            hierarchy_threshold=settings.synthesis_hierarchy_threshold,
            group_size=settings.synthesis_group_size,
            partial_max_tokens=settings.llm_max_tokens_partial,
            final_max_tokens=settings.llm_max_tokens_final,
        ),
        qa=QAService(llm),
        telegraph=TelegraphService(settings),
        contexts={},
        summary_queue=asyncio.Queue(),
        summary_queue_lock=asyncio.Lock(),
        summary_worker_task=None,
        summary_active_job=None,
        summary_next_sequence=0,
        summary_status_messages={},
        summary_status_base_texts={},
        summary_status_parse_modes={},
        summary_status_disable_previews={},
        bot=bot,
        groq_whisper=groq_whisper,
        transcription_queue=asyncio.Queue(),
        transcription_queue_lock=asyncio.Lock(),
        transcription_worker_task=None,
        transcription_active_job=None,
        summary_cache=summary_cache,
        channel_posts=channel_posts,
        tags_catalog=tags_catalog,
        digests=digest_store,
    )

    scheduler_task: asyncio.Task[None] | None = None
    if settings.monitoring_enabled:
        monitoring_config = MonitoringConfig(settings.monitoring_config_path)
        monitoring_config.load()
        monitoring_state = MonitoringState(settings.monitoring_state_path)
        monitoring_state.load()

        async def _enqueue(candidate, channel):
            await enqueue_scheduled_candidate(candidate, channel, services)

        async def _check_llm() -> tuple[bool, str]:
            return await health_check_with_reason(services.llm)

        services.monitoring = MonitoringService(
            config=monitoring_config,
            state=monitoring_state,
            youtube=services.youtube,
            enqueue=_enqueue,
        )
        scheduler_task = asyncio.create_task(
            run_monitoring_scheduler(services.monitoring, llm_check=_check_llm),
            name="monitoring-scheduler",
        )
        logger.info(
            "monitoring.boot enabled=true config=%s state=%s target_chat_id=%s provider=%s",
            settings.monitoring_config_path,
            settings.monitoring_state_path,
            settings.monitoring_target_chat_id,
            settings.llm_provider,
        )
    else:
        logger.info("monitoring.boot enabled=false")

    await configure_bot_commands(bot, settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(services))
    try:
        await dispatcher.start_polling(bot)
    finally:
        if scheduler_task is not None and not scheduler_task.done():
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("monitoring.scheduler.shutdown_failed")


if __name__ == "__main__":
    asyncio.run(main())
