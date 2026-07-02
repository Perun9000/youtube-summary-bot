from __future__ import annotations

import json
import logging
import time
from pathlib import Path

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
        # Token resolution priority (high → low):
        #   1. Explicit env var ``TELEGRAPH_ACCESS_TOKEN`` (settings.telegraph_access_token).
        #   2. ``data/telegraph_token.txt`` — persisted across container restarts;
        #      this is what a freshly-created account gets stored as so that
        #      ``editPage`` can target pages we created in previous sessions.
        #   3. Auto-created on first publish() call (and saved to (2)).
        self._access_token: str | None = settings.telegraph_access_token
        if not self._access_token:
            self._access_token = _load_persisted_token(self._token_file_path())

    def _token_file_path(self) -> Path:
        return self._settings.bot_data_dir / "telegraph_token.txt"

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

    async def edit(
        self,
        page_url_or_path: str,
        *,
        title: str,
        video_url: str,
        summary: Summary,
        transcript_url: str | None = None,
        top_comments: list[VideoComment] | None = None,
    ) -> str:
        """Re-publish an existing summary page with updated content.

        Used when serving a cached summary and we want to refresh the
        "Топ-комментарии" section with current YouTube state. Returns the
        same URL the page already has — Telegra.ph keeps the path stable.

        Caveat: ``editPage`` works **only with the same access_token that
        created the page**. Pages published by previous bot incarnations (with
        a now-lost token) cannot be edited; the API will return
        ``PAGE_ACCESS_DENIED`` in that case.
        """
        if not self._access_token:
            self._access_token = await self._create_account()
        page_path = _extract_telegraph_path(page_url_or_path)
        started = time.monotonic()
        logger.info(
            "telegraph.edit.start path=%s comments=%s",
            page_path, len(top_comments) if top_comments else 0,
        )
        content = _summary_to_nodes(
            video_url, summary, transcript_url=transcript_url, top_comments=top_comments,
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.telegra.ph/editPage",
                data={
                    "access_token": self._access_token,
                    "path": page_path,
                    "title": title[:255] or "YouTube summary",
                    "author_name": self._settings.telegraph_author_name,
                    "content": json.dumps(content, ensure_ascii=False),
                    "return_content": "false",
                },
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "Telegra.ph editPage failed"))
            page_url = str(data["result"]["url"])
            logger.info(
                "telegraph.edit.done duration_sec=%.1f url=%s",
                time.monotonic() - started, page_url,
            )
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
            token = str(data["result"]["access_token"])
            # Persist so subsequent restarts can keep editing pages we created.
            _persist_token(self._token_file_path(), token)
            logger.info("telegraph.account.create.done token_persisted=true")
            return token


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
    ]

    # Теги — отдельный <p><i>...</i></p> сразу после заголовочных ссылок,
    # чтобы читатель сразу видел: «о чём это, кто и формат». Telegra.ph
    # автоматически делает текст вида ``#тег`` некликабельным (это просто
    # текст в браузере), но визуально читается как тег-блок.
    tags_text = _tags_inline_for_telegraph(getattr(summary, "tags", None))
    if tags_text:
        nodes.append({
            "tag": "p",
            "children": [{"tag": "i", "children": [tags_text]}],
        })

    # Executive summary + подробный разбор тезисов. Блока с короткими
    # тезисами-буллетами больше нет.
    nodes.extend([
        {"tag": "h3", "children": ["Executive summary"]},
        {"tag": "p", "children": [summary.overview]},
        {"tag": "h3", "children": ["Ключевые тезисы"]},
    ])

    for chapter in summary.chapters:
        heading = chapter.title.strip() or "Тезис"
        nodes.append({"tag": "h4", "children": [heading]})
        for paragraph in [part.strip() for part in chapter.notes.split("\n\n") if part.strip()]:
            nodes.append({"tag": "p", "children": [paragraph]})

    if not summary.chapters:
        # Если модель почему-то не отдала chapters — не оставляем пустой раздел,
        # показываем сырой ответ как fallback.
        nodes.append({"tag": "p", "children": [summary.raw_text]})

    if top_comments:
        nodes.append({"tag": "h3", "children": ["Топ-комментарии"]})
        for c in top_comments:
            # Header line with author + like count + pinned marker
            pinned = "📌 " if c.is_pinned else ""
            likes = _compact_count(c.like_count)
            header_text = f"{pinned}{c.author} · ❤ {likes}".strip()
            nodes.append(
                {"tag": "p", "children": [{"tag": "b", "children": [header_text]}]}
            )
            # Comment body — Telegra.ph supports <blockquote>, gives nice visual
            # separation between meta-line and the actual comment.
            nodes.append({"tag": "blockquote", "children": [c.text]})

    return nodes


def _load_persisted_token(path: Path) -> str | None:
    """Load a Telegraph access_token saved in a previous run, or return None."""
    try:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                logger.info("telegraph.token.loaded path=%s", path)
                return token
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegraph.token.load_failed path=%s error=%s", path, exc)
    return None


def _persist_token(path: Path, token: str) -> None:
    """Save Telegraph access_token to disk (atomically). Best-effort, never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(token, encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegraph.token.save_failed path=%s error=%s", path, exc)


def _extract_telegraph_path(page_url_or_path: str) -> str:
    """Take last segment of telegra.ph URL or return path as-is.

    Examples:
        https://telegra.ph/Some-Title-04-26  → 'Some-Title-04-26'
        Some-Title-04-26                     → 'Some-Title-04-26'
    """
    s = (page_url_or_path or "").strip().rstrip("/")
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    return s


def _tags_inline_for_telegraph(tags) -> str:
    """Build the ``🏷 #тема #Гость #Ведущий #формат #Канал`` line for Telegra.ph.

    Принимает ``SummaryTags | None``. Возвращает пустую строку, если тегов
    вообще нет — caller тогда не добавляет блок в nodes. Tag-syntax тут
    тот же, что в Telegram-сообщении (для визуальной согласованности),
    хотя в Telegra.ph они не кликабельны.
    """
    if tags is None:
        return ""
    parts: list[str] = []
    topic = getattr(tags, "topic", "")
    if topic:
        parts.append(f"#{topic}")
    for sp in getattr(tags, "speakers", ()) or ():
        if sp:
            parts.append(f"#{sp}")
    for host in getattr(tags, "hosts", ()) or ():
        if host:
            parts.append(f"#{host}")
    fmt = getattr(tags, "format", "")
    if fmt:
        parts.append(f"#{fmt}")
    channel = getattr(tags, "channel", "")
    if channel:
        parts.append(f"#{channel}")
    if not parts:
        return ""
    return "🏷 " + " ".join(parts)


def _compact_count(count: int) -> str:
    """Inline copy of bot_handlers._format_compact_count to avoid circular import."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        if count < 10_000:
            value = round(count / 1000, 1)
            return f"{value:g}к"
        return f"{count // 1000}к"
    value = round(count / 1_000_000, 1)
    return f"{value:g}м"


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
