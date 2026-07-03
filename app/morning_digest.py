"""Утренний дайджест мониторинга.

Scheduled-саммари не шлются отдельными сообщениями — складываются в таблицу
``morning_digest_items``. Когда пачка суточного скана дообработана (в очереди
не осталось scheduled-задач), бот один раз зовёт LLM отранжировать видео по
интересам пользователя (interests + whitelists из monitoring.yaml) и шлёт одно
сообщение со списком: score, ссылка на конспект, «почему стоит внимания».
Если LLM недоступна — фолбэк: неранжированный список, дайджест всё равно уходит.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from app.db import Database
from app.utils import escape_html


logger = logging.getLogger(__name__)

MAX_DIGEST_MESSAGE_CHARS = 4000

RANK_SYSTEM_PROMPT = (
    "Ты помогаешь отбирать YouTube-видео по интересам пользователя. "
    "Отвечай строго JSON-массивом без пояснений."
)

RANK_PROMPT_TEMPLATE = """
Интересы пользователя: {interests}

Ниже — новые видео за сутки (id, название, канал, краткий обзор, теги).
Оцени каждое по релевантности интересам от 0 до 10 и одним коротким
предложением объясни, почему видео стоит (или не стоит) внимания.

Видео:
{items_block}

Ответ — строго JSON-массив вида:
[{{"video_id": "...", "score": 0, "reason": "..."}}]
""".strip()


@dataclass(frozen=True)
class MorningDigestItem:
    video_id: str
    title: str
    channel_name: str
    telegraph_url: str
    overview: str
    tags_line: str
    duration_sec: float
    created_at_unix: float


class MorningDigestStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, item: MorningDigestItem) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO morning_digest_items"
            "(video_id, title, channel_name, telegraph_url, overview, tags_line, duration_sec, created_at_unix, sent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (item.video_id, item.title, item.channel_name, item.telegraph_url,
             item.overview, item.tags_line, item.duration_sec, item.created_at_unix),
        )

    def unsent(self) -> list[MorningDigestItem]:
        rows = self._db.query(
            "SELECT * FROM morning_digest_items WHERE sent = 0 ORDER BY created_at_unix"
        )
        return [
            MorningDigestItem(
                video_id=r["video_id"], title=r["title"], channel_name=r["channel_name"],
                telegraph_url=r["telegraph_url"], overview=r["overview"], tags_line=r["tags_line"],
                duration_sec=r["duration_sec"], created_at_unix=r["created_at_unix"],
            )
            for r in rows
        ]

    def mark_sent(self, video_ids: list[str]) -> None:
        self._db.executemany(
            "UPDATE morning_digest_items SET sent = 1 WHERE video_id = ?",
            [(vid,) for vid in video_ids],
        )


def build_rank_prompt(items: list[MorningDigestItem], interests: list[str]) -> str:
    interests_text = ", ".join(interests) if interests else "не заданы (оценивай общую содержательность)"
    lines = []
    for it in items:
        overview = it.overview[:600]
        lines.append(
            f"- id: {it.video_id}\n  название: {it.title}\n  канал: {it.channel_name}\n"
            f"  обзор: {overview}\n  теги: {it.tags_line or '—'}"
        )
    return RANK_PROMPT_TEMPLATE.format(interests=interests_text, items_block="\n".join(lines))


def parse_rank_response(raw: str, valid_ids: set[str]) -> dict[str, tuple[int, str]]:
    """Разобрать JSON-ответ ранжирования. Мусор → пустой dict (fallback-режим)."""
    cleaned = raw.strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list):
        return {}
    ranks: dict[str, tuple[int, str]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("video_id") or "")
        if vid not in valid_ids:
            continue
        try:
            score = int(entry.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(10, score))
        reason = str(entry.get("reason") or "").strip()
        ranks[vid] = (score, reason)
    return ranks


def render_morning_digest(
    items: list[MorningDigestItem], ranks: dict[str, tuple[int, str]]
) -> str:
    """HTML-сообщение дайджеста, ≤4000 символов.

    Порядок: сначала видео с оценкой (по убыванию score), затем без оценки
    (fallback, если LLM не отранжировала). Не влезающие в бюджет строки
    молча отбрасываются с конца — самое релевантное всегда наверху и внутри.

    Отклонение от исходного рендера (Task 10, деградация Telegra.ph): если
    у item'а пустой ``telegraph_url`` (страница не была опубликована),
    ссылкой служит сам YouTube-ролик — иначе получилась бы битая ссылка
    ``href=""``.
    """
    ranked = sorted(
        (it for it in items if it.video_id in ranks),
        key=lambda it: ranks[it.video_id][0],
        reverse=True,
    )
    unranked = [it for it in items if it.video_id not in ranks]
    ordered = [*ranked, *unranked]

    head = f"📬 <b>Дайджест мониторинга</b> — новых видео: {len(items)}"
    parts: list[str] = [head]
    used = len(head)
    for it in ordered:
        title = escape_html(it.title or it.video_id)
        link = it.telegraph_url or f"https://www.youtube.com/watch?v={it.video_id}"
        url = escape_html(link)
        channel = escape_html(it.channel_name or "")
        rank = ranks.get(it.video_id)
        if rank is not None:
            score, reason = rank
            line = f"\n\n<b>{score}/10</b> · <a href=\"{url}\">{title}</a>"
            if channel:
                line += f" · {channel}"
            if reason:
                line += f"\n<i>{escape_html(reason)}</i>"
        else:
            line = f"\n\n• <a href=\"{url}\">{title}</a>" + (f" · {channel}" if channel else "")
        if used + len(line) > MAX_DIGEST_MESSAGE_CHARS:
            break
        parts.append(line)
        used += len(line)
    return "".join(parts)


async def maybe_send_morning_digest(services) -> bool:
    """Отправить дайджест, если пачка scheduled-задач дообработана.

    Вызывается из queue-worker'а после каждой завершённой задачи и один раз
    на старте бота (на случай рестарта между «всё сгенерили» и «отправили»).
    """
    store = services.morning_digest
    if store is None or services.job_store is None:
        return False
    if services.job_store.scheduled_pending_count() > 0:
        return False
    items = store.unsent()
    if not items:
        return False
    target_chat_id = services.settings.monitoring_target_chat_id
    if target_chat_id is None or services.bot is None:
        return False

    interests: list[str] = []
    if services.monitoring is not None:
        rules = services.monitoring.rules
        interests = [*rules.interests, *rules.shows_whitelist, *rules.experts_whitelist]

    ranks: dict[str, tuple[int, str]] = {}
    try:
        raw = await services.llm.generate(
            build_rank_prompt(items, interests), system=RANK_SYSTEM_PROMPT
        )
        ranks = parse_rank_response(raw or "", {it.video_id for it in items})
        logger.info("morning_digest.ranked items=%s ranked=%s", len(items), len(ranks))
    except Exception:
        logger.exception("morning_digest.rank_failed — шлём без ранжирования")

    text = render_morning_digest(items, ranks)
    try:
        await services.bot.send_message(
            chat_id=target_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=True,
        )
    except Exception:
        logger.exception("morning_digest.send_failed items=%s", len(items))
        return False
    store.mark_sent([it.video_id for it in items])
    logger.info("morning_digest.sent items=%s chat_id=%s", len(items), target_chat_id)
    return True
