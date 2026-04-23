from __future__ import annotations

import json
import logging
import time

import httpx

from app.config import Settings
from app.models import Summary


logger = logging.getLogger(__name__)


class TelegraphService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._access_token = settings.telegraph_access_token

    async def publish(self, title: str, url: str, summary: Summary) -> str:
        started = time.monotonic()
        logger.info(
            "telegraph.publish.start title=%r key_points=%s chapters=%s",
            title,
            len(summary.key_points),
            len(summary.chapters),
        )
        if not self._access_token:
            self._access_token = await self._create_account()

        content = _summary_to_nodes(url, summary)
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.telegra.ph/createPage",
                data={
                    "access_token": self._access_token,
                    "title": title[:255] or "YouTube summary",
                    "author_name": self._settings.telegraph_author_name,
                    "content": json.dumps(content, ensure_ascii=False),
                    "return_content": "false",
                },
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "Telegra.ph createPage failed"))
            page_url = str(data["result"]["url"])
            logger.info("telegraph.publish.done duration_sec=%.1f url=%s", time.monotonic() - started, page_url)
            return page_url

    async def _create_account(self) -> str:
        logger.info("telegraph.account.create.start")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.telegra.ph/createAccount",
                data={
                    "short_name": "yt_summary_bot",
                    "author_name": self._settings.telegraph_author_name,
                },
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "Telegra.ph createAccount failed"))
            logger.info("telegraph.account.create.done")
            return str(data["result"]["access_token"])


def _summary_to_nodes(url: str, summary: Summary) -> list[dict | str]:
    nodes: list[dict | str] = [
        {"tag": "p", "children": [{"tag": "a", "attrs": {"href": url}, "children": ["Оригинальный ролик"]}]},
        {"tag": "h3", "children": ["Обзор"]},
        {"tag": "p", "children": [summary.overview]},
        {"tag": "h3", "children": ["Ключевые тезисы"]},
        {"tag": "ul", "children": [{"tag": "li", "children": [point]} for point in summary.key_points]},
        {"tag": "h3", "children": ["Тезисы подробно"]},
    ]

    for chapter in summary.chapters:
        heading = chapter.title.strip() or "Тезис"
        nodes.append({"tag": "h4", "children": [heading]})
        for paragraph in [part.strip() for part in chapter.notes.split("\n\n") if part.strip()]:
            nodes.append({"tag": "p", "children": [paragraph]})

    if not summary.chapters:
        nodes.append({"tag": "p", "children": [summary.raw_text]})

    return nodes
