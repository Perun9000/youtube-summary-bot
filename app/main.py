from __future__ import annotations

import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault, MenuButtonCommands

from app.bot_handlers import Services, build_router, enqueue_scheduled_candidate
from app.config import load_settings
from app.llm_client import create_llm_client
from app.monitoring_config import MonitoringConfig
from app.monitoring_service import MonitoringService
from app.monitoring_state import MonitoringState
from app.qa_service import QAService
from app.scheduler_service import run_monitoring_scheduler
from app.summarizer import Summarizer
from app.telegraph_service import TelegraphService
from app.whisper_service import WhisperService
from app.youtube_service import YouTubeService


logger = logging.getLogger(__name__)


BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Начать работу"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="reset", description="Забыть текущий ролик"),
    BotCommand(command="models", description="Доступные LLM-модели"),
    BotCommand(command="model", description="Текущая модель бота"),
    BotCommand(command="queue", description="Очередь summary"),
    BotCommand(command="stop", description="Остановить генерацию"),
]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_FILE_NAME = "bot.log"
LOG_FILE_ROTATION_DAYS = 7
LOG_FILE_BACKUP_COUNT = 8


async def configure_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
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

    services = Services(
        settings=settings,
        llm=llm,
        youtube=YouTubeService(settings),
        whisper=WhisperService(settings),
        summarizer=Summarizer(
            llm,
            hierarchy_threshold=settings.synthesis_hierarchy_threshold,
            group_size=settings.synthesis_group_size,
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
    )

    scheduler_task: asyncio.Task[None] | None = None
    if settings.monitoring_enabled:
        monitoring_config = MonitoringConfig(settings.monitoring_config_path)
        monitoring_config.load()
        monitoring_state = MonitoringState(settings.monitoring_state_path)
        monitoring_state.load()

        async def _enqueue(candidate, channel):
            await enqueue_scheduled_candidate(candidate, channel, services)

        services.monitoring = MonitoringService(
            config=monitoring_config,
            state=monitoring_state,
            youtube=services.youtube,
            enqueue=_enqueue,
        )
        scheduler_task = asyncio.create_task(
            run_monitoring_scheduler(services.monitoring),
            name="monitoring-scheduler",
        )
        logger.info(
            "monitoring.boot enabled=true config=%s state=%s target_chat_id=%s",
            settings.monitoring_config_path,
            settings.monitoring_state_path,
            settings.monitoring_target_chat_id,
        )
    else:
        logger.info("monitoring.boot enabled=false")

    await configure_bot_commands(bot)
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
