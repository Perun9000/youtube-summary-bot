from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.monitoring_service import MonitoringService


logger = logging.getLogger(__name__)

FALLBACK_TZ = "UTC"


async def run_monitoring_scheduler(
    service: MonitoringService,
    llm_check: Callable[[], Awaitable[tuple[bool, str]]] | None = None,
) -> None:
    logger.info("scheduler.start")
    try:
        while True:
            # Re-read config every tick so edits to monitoring.yaml take effect
            # on the next sleep (no need to restart the bot).
            rules = service.config.rules
            tz = _resolve_tz(rules.scan_tz)
            scan_hour, scan_minute = _parse_scan_time(rules.scan_time)

            now = datetime.now(tz)
            next_run = now.replace(hour=scan_hour, minute=scan_minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            sleep_sec = max(1.0, (next_run - now).total_seconds())

            logger.info(
                "scheduler.sleep until=%s tz=%s sleep_sec=%.0f",
                next_run.isoformat(),
                rules.scan_tz,
                sleep_sec,
            )
            try:
                await asyncio.sleep(sleep_sec)
            except asyncio.CancelledError:
                logger.info("scheduler.cancelled")
                raise

            logger.info("scheduler.tick start")
            try:
                await service.run_scan(llm_check=llm_check)
            except Exception:
                logger.exception("scheduler.scan.failed")
            # Tiny guard so that a fast-running scan doesn't retrigger within the same minute.
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("scheduler.loop.crashed")


def _resolve_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("scheduler.tz.unknown name=%r fallback=%s", name, FALLBACK_TZ)
        return ZoneInfo(FALLBACK_TZ)


def _parse_scan_time(raw: str) -> tuple[int, int]:
    try:
        parsed = dtime.fromisoformat(raw)
        return parsed.hour, parsed.minute
    except ValueError:
        logger.warning("scheduler.scan_time.bad raw=%r fallback=22:00", raw)
        return 22, 0
