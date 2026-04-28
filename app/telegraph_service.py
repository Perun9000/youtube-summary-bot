from __future__ import annotations

import json
import logging
import time

import httpx

from app.config import Settings
from app.models import Summary, TranscriptSegment, VideoComment
from app.utils import format_ts


logger = logging.getLogger(__name__)

# Telegra.ph limit on page content is ~64 KB when serialised as JSON.
# Each YouTube-sourced segment turns into a node with timestamp-link tag +
# href attribute, which adds ~150 bytes of JSON overhead per segment on top
# of the actual text. So we have to budget by the *serialised* size rather
# than plain text length. Set a comfortable ceiling under the API limit.
TRANSCRIPT_PAGE_JSON_BUDGET_BYTES = 60000


class TelegraphService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._access_token = settings.telegraph_access_token

    async def publish(
        self,
        title: str,
        url: str,
        summary: Summary,
        transcript_url: str | None = None,
        top_comments: list[VideoComment] | None = None,
    ) -> str:
        started = time.monotonic()
        logger.info(
            "telegraph.publish.start title=%r key_points=%s chapters=%s transcript_url=%s comments=%s",
            title,
            len(summary.key_points),
            len(summary.chapters),
            transcript_url,
            len(top_comments) if top_comments else 0,
        )
        if not self._access_token:
            self._access_token = await self._create_account()

        content = _summary_to_nodes(
            url, summary, transcript_url=transcript_url, top_comments=top_comments,
        )
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

    async def publish_transcript(
        self,
        *,
        title: str,
        video_url: str,
        video_id: str,
        segments: list[TranscriptSegment],
        source: str,
    ) -> str:
        started = time.monotonic()
        logger.info(
            "telegraph.publish_transcript.start video_id=%s segments=%s source=%s",
            video_id,
            len(segments),
            source,
        )
        if not self._access_token:
            self._access_token = await self._create_account()

        nodes, kept, truncated = _transcript_to_nodes(
            video_url=video_url,
            video_id=video_id,
            segments=segments,
            source=source,
        )
        page_title = f"Транскрипт — {title}".strip() or "YouTube transcript"
        page_title = page_title[:255]

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.telegra.ph/createPage",
                data={
                    "access_token": self._access_token,
                    "title": page_title,
                    "author_name": self._settings.telegraph_author_name,
                    "content": json.dumps(nodes, ensure_ascii=False),
                    "return_content": "false",
                },
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "Telegra.ph createPage (transcript) failed"))
            page_url = str(data["result"]["url"])
            logger.info(
                "telegraph.publish_transcript.done duration_sec=%.1f url=%s kept=%s total=%s truncated=%s",
                time.monotonic() - started,
                page_url,
                kept,
                len(segments),
                truncated,
            )
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


def _summary_to_nodes(
    url: str,
    summary: Summary,
    transcript_url: str | None = None,
    top_comments: list[VideoComment] | None = None,
) -> list[dict | str]:
    header_children: list[dict | str] = [
        {"tag": "a", "attrs": {"href": url}, "children": ["Оригинальный ролик"]},
    ]
    if transcript_url:
        header_children.append(" · ")
        header_children.append(
            {"tag": "a", "attrs": {"href": transcript_url}, "children": ["Полный транскрипт"]}
        )

    nodes: list[dict | str] = [
        {"tag": "p", "children": header_children},
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

    if top_comments:
        nodes.append({"tag": "h3", "children": ["Топ-комментарии"]})
        for c in top_comments:
            # Header line with author + like count + pinned marker
            pinned = "📌 " if c.is_pinned else ""
            header_text = f"{pinned}{c.author} · ❤ {c.like_count}".strip()
            nodes.append(
                {"tag": "p", "children": [{"tag": "b", "children": [header_text]}]}
            )
            # Comment body — Telegra.ph supports <blockquote>, gives nice visual
            # separation between meta-line and the actual comment.
            nodes.append({"tag": "blockquote", "children": [c.text]})

    return nodes


def _transcript_to_nodes(
    *,
    video_url: str,
    video_id: str,
    segments: list[TranscriptSegment],
    source: str,
) -> tuple[list[dict | str], int, bool]:
    """Build Telegra.ph nodes for a transcript page, capped by JSON byte budget.

    Telegra.ph's createPage rejects payloads above ~64 KB with CONTENT_TOO_BIG.
    YouTube-sourced timestamp links add ~150 B of JSON overhead per segment,
    so we accumulate the actual serialised size as we go and stop when we'd
    spill over the budget — keeping plenty of headroom for the truncation
    note + JSON envelope.
    """
    header_node: dict = {
        "tag": "p",
        "children": [
            {"tag": "a", "attrs": {"href": video_url}, "children": ["Оригинальный ролик"]},
        ],
    }
    nodes: list[dict | str] = [header_node]
    used_bytes = len(json.dumps(header_node, ensure_ascii=False).encode("utf-8"))

    kept = 0
    truncated = False
    total_non_empty = 0
    # Reserve a slice of the budget for the truncation footer (~250 B for
    # an em-tag + Russian text + JSON wrapping), so we still have room to
    # tell the user we cut something.
    soft_limit = TRANSCRIPT_PAGE_JSON_BUDGET_BYTES - 400

    for segment in segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        total_non_empty += 1
        ts_label = f"[{format_ts(segment.start)}]"

        if source == "youtube":
            start_seconds = int(max(0, segment.start))
            ts_href = f"https://www.youtube.com/watch?v={video_id}&t={start_seconds}s"
            node = {
                "tag": "p",
                "children": [
                    {"tag": "a", "attrs": {"href": ts_href}, "children": [ts_label]},
                    f" {text}",
                ],
            }
        else:
            node = {"tag": "p", "children": [f"{ts_label} {text}"]}

        node_bytes = len(json.dumps(node, ensure_ascii=False).encode("utf-8"))
        if used_bytes + node_bytes > soft_limit:
            truncated = True
            continue

        nodes.append(node)
        used_bytes += node_bytes
        kept += 1

    if truncated:
        note = (
            f"Транскрипт усечён: показано {kept} из {total_non_empty} фрагментов. "
            "Полный текст сохранён в виде файла на сервере бота."
        )
        nodes.append({"tag": "p", "children": [{"tag": "em", "children": [note]}]})

    return nodes, kept, truncated
