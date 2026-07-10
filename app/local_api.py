"""Локальный HTTP API для «тихой» постановки роликов из browser-extension.

Порт публикуется docker-compose'ом только на 127.0.0.1 хоста; авторизация —
статический токен LOCAL_API_TOKEN (hmac.compare_digest). CORS/PNA-заголовки
отдаются для https://www.youtube.com — страховка на случай fetch из
content-script (основной путь расширения — background service worker,
которому CORS не нужен).
"""
from __future__ import annotations

import hmac
import logging

from aiohttp import web

from app.queue_service import enqueue_local_api_job
from app.services_container import YOUTUBE_VIDEO_ID_RE

logger = logging.getLogger(__name__)

ALLOWED_ORIGIN = "https://www.youtube.com"


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Auth-Token",
        "Access-Control-Allow-Private-Network": "true",
    }


def create_local_api_app(services) -> web.Application:
    token = services.settings.local_api_token

    async def enqueue(request: web.Request) -> web.Response:
        supplied = request.headers.get("X-Auth-Token", "")
        if not (supplied and hmac.compare_digest(supplied, token)):
            return web.json_response({"error": "unauthorized"}, status=401, headers=_cors_headers())
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad_json"}, status=400, headers=_cors_headers())
        video_id = str(payload.get("video_id", ""))
        if not YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
            return web.json_response({"error": "bad_video_id"}, status=400, headers=_cors_headers())
        try:
            status = await enqueue_local_api_job(video_id, services)
        except Exception:  # noqa: BLE001
            logger.exception("local_api.enqueue_failed video_id=%s", video_id)
            return web.json_response({"error": "internal"}, status=500, headers=_cors_headers())
        return web.json_response({"status": status}, headers=_cors_headers())

    async def preflight(request: web.Request) -> web.Response:
        return web.Response(status=204, headers=_cors_headers())

    app = web.Application()
    app.router.add_post("/enqueue", enqueue)
    app.router.add_options("/enqueue", preflight)
    return app


async def start_local_api(services) -> web.AppRunner | None:
    s = services.settings
    if not s.local_api_token:
        logger.info("local_api.boot enabled=false reason=no_token")
        return None
    if s.owner_user_id is None:
        logger.warning("local_api.boot enabled=false reason=no_owner_user_id")
        return None
    runner = web.AppRunner(create_local_api_app(services))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", s.local_api_port)
    await site.start()
    logger.info("local_api.boot enabled=true port=%s", s.local_api_port)
    return runner


async def stop_local_api(runner: web.AppRunner | None) -> None:
    if runner is not None:
        await runner.cleanup()
