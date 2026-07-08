from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from app.config import Settings
from app.i18n import t
from app.models import Summary, VideoComment


logger = logging.getLogger(__name__)

RETRY_DELAYS_SEC: tuple[float, ...] = (2.0, 8.0)


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

    async def _post_with_retries(self, endpoint: str, data: dict) -> dict:
        """POST к api.telegra.ph с ретраями на сетевые ошибки и 5xx.

        Часовая генерация саммари не должна пропадать из-за секундного
        сбоя HTTP — три попытки с паузами 2s/8s. 4xx (наши ошибки данных)
        не ретраим.
        """
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*RETRY_DELAYS_SEC, None), start=1):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(f"https://api.telegra.ph/{endpoint}", data=data)
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code}", request=response.request, response=response
                    )
                # 4xx — наши ошибки данных (например, PAGE_ACCESS_DENIED),
                # ретраить их бессмысленно: результат не изменится. Поднимаем
                # сразу, не через retry-цикл.
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code < 500:
                    raise
                last_exc = exc
                if delay is None:
                    break
                logger.warning(
                    "telegraph.retry endpoint=%s attempt=%s error=%s", endpoint, attempt, exc
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:
                last_exc = exc
                if delay is None:
                    break
                logger.warning(
                    "telegraph.retry endpoint=%s attempt=%s error=%s", endpoint, attempt, exc
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def publish(
        self,
        title: str,
        url: str,
        summary: Summary,
        top_comments: list[VideoComment] | None = None,
        lang: str = "ru",
    ) -> str:
        started = time.monotonic()
        logger.info(
            "telegraph.publish.start title=%r key_points=%s chapters=%s comments=%s",
            title,
            len(summary.key_points),
            len(summary.chapters),
            len(top_comments) if top_comments else 0,
        )
        if not self._access_token:
            self._access_token = await self._create_account()

        content = _summary_to_nodes(
            url, summary, top_comments=top_comments, lang=lang,
        )
        data = await self._post_with_retries(
            "createPage",
            {
                "access_token": self._access_token,
                "title": title[:255] or "YouTube summary",
                "author_name": self._settings.telegraph_author_name,
                "content": json.dumps(content, ensure_ascii=False),
                "return_content": "false",
            },
        )
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
        top_comments: list[VideoComment] | None = None,
        lang: str = "ru",
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
            video_url, summary, top_comments=top_comments, lang=lang,
        )
        data = await self._post_with_retries(
            "editPage",
            {
                "access_token": self._access_token,
                "path": page_path,
                "title": title[:255] or "YouTube summary",
                "author_name": self._settings.telegraph_author_name,
                "content": json.dumps(content, ensure_ascii=False),
                "return_content": "false",
            },
        )
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "Telegra.ph editPage failed"))
        page_url = str(data["result"]["url"])
        logger.info(
            "telegraph.edit.done duration_sec=%.1f url=%s",
            time.monotonic() - started, page_url,
        )
        return page_url

    async def _create_account(self) -> str:
        logger.info("telegraph.account.create.start")
        data = await self._post_with_retries(
            "createAccount",
            {
                "short_name": "yt_summary_bot",
                "author_name": self._settings.telegraph_author_name,
            },
        )
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
    top_comments: list[VideoComment] | None = None,
    lang: str = "ru",
) -> list[dict | str]:
    header_children: list[dict | str] = [
        {"tag": "a", "attrs": {"href": url}, "children": [t("telegraph.original", lang)]},
    ]

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

    # Executive summary — рендерим всегда.
    nodes.extend([
        {"tag": "h3", "children": [t("telegraph.exec_summary", lang)]},
        {"tag": "p", "children": [summary.overview]},
    ])

    # Подробный разбор тезисов — только если модель отдала валидные chapters.
    # Если chapters пустые (обычно — модель вернула повреждённый JSON, и
    # _summary_from_damaged_json смог достать только overview), заголовок
    # «Ключевые тезисы» вообще не показываем. Раньше здесь дампился raw_text —
    # но это сырой JSON модели, читателю от него только хуже.
    if summary.chapters:
        nodes.append({"tag": "h3", "children": [t("telegraph.chapters", lang)]})
        for chapter in summary.chapters:
            heading = chapter.title.strip() or t("telegraph.chapter_placeholder", lang)
            nodes.append({"tag": "h4", "children": [heading]})
            for paragraph in [part.strip() for part in chapter.notes.split("\n\n") if part.strip()]:
                nodes.append({"tag": "p", "children": [paragraph]})
    else:
        nodes.append({
            "tag": "p",
            "children": [
                {"tag": "em", "children": [t("telegraph.chapters_failed", lang)]},
            ],
        })

    if top_comments:
        nodes.append({"tag": "h3", "children": [t("telegraph.comments", lang)]})
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
