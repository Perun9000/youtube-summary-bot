from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import time

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.digest_service import DigestEntry, update_pin_for_user
from app.i18n import t
from app.morning_digest import MorningDigestItem
from app.summary_cache import CachedSummary
from app.tags_catalog import TagsCatalog
from app.models import Summary, SummaryTags, VideoComment
from app.monitoring_service import format_spans_for_humans
from app.utils import escape_html, extract_video_id

from app.services_container import (
    MAX_TELEGRAM_MESSAGE_CHARS,
    TOP_COMMENT_MAX_CHARS,
    Services,
    SummaryJob,
)
from app.status_messages import _fit_telegram_message


logger = logging.getLogger(__name__)


def _format_telegram_summary(
    title: str,
    video_url: str,
    summary: Summary,
    telegraph_url: str,
    channel_name: str,
    channel_url: str,
    scheduled: bool = False,
    segment_spans: list[tuple[float, float]] | None = None,
    expert_matches: list[str] | None = None,
    top_comment: VideoComment | None = None,
    bot_username: str | None = None,
    lang: str = "ru",
) -> str:
    if channel_name and channel_url:
        channel_line = t(
            "summary.new_video_channel_link", lang,
            url=escape_html(channel_url), channel=escape_html(channel_name),
        )
    elif channel_name:
        channel_line = t("summary.new_video_channel", lang, channel=escape_html(channel_name))
    else:
        channel_line = t("summary.new_video", lang)

    title_line = f'<b><a href="{escape_html(video_url)}">{escape_html(title)}</a></b>'

    segment_line = ""
    if scheduled and segment_spans:
        spans_text = format_spans_for_humans(segment_spans)
        if expert_matches:
            experts_text = ", ".join(expert_matches)
            segment_line = (
                f"<i>Фрагмент с участием: {escape_html(experts_text)} "
                f"({escape_html(spans_text)})</i>"
            )
        else:
            segment_line = f"<i>Фрагмент ролика: {escape_html(spans_text)}</i>"

    overview_line = f"{t('summary.about', lang)}\n{escape_html(summary.overview)}"
    reading_line = t("summary.reading_time", lang, minutes=_estimate_reading_time_minutes(summary))

    # Ссылка на полный конспект живёт и в inline-кнопке (_build_summary_keyboard),
    # и в теле сообщения: кнопка при пересылке сообщения не сохраняется,
    # а гиперссылка в тексте — сохраняется. При деградации Telegraph
    # (telegraph_url == "") строка просто не добавляется.
    telegraph_line = ""
    if telegraph_url:
        telegraph_line = (
            f'🔮 <a href="{escape_html(telegraph_url)}">{t("summary.details_link", lang)}</a>'
        )

    blocks = [channel_line, title_line]
    if segment_line:
        blocks.append(segment_line)
    blocks.extend([overview_line, reading_line])
    if telegraph_line:
        blocks.append(telegraph_line)

    tags_line = _format_tags_line(summary.tags)
    if tags_line:
        blocks.append(tags_line)

    # Подпись со ссылкой на бота. Именно @-упоминание видимым текстом (а не
    # <a href> на слове): при копировании текста сообщения в комментарии
    # Telegram href теряется, а @mention остаётся и авто-линкуется.
    signature_line = t("summary.made_by", lang, username=bot_username) if bot_username else ""

    if top_comment is not None:
        base_text = "\n\n".join(blocks)
        separator_len = 2 if base_text else 0
        signature_cost = (len(signature_line) + 2) if signature_line else 0
        available_chars = (
            MAX_TELEGRAM_MESSAGE_CHARS - len(base_text) - separator_len - signature_cost
        )
        top_comment_line = _format_top_comment_line(top_comment, available_chars, lang)
        if top_comment_line:
            blocks.append(top_comment_line)

    if signature_line:
        blocks.append(signature_line)

    return _fit_telegram_message("\n\n".join(blocks))
def _format_tags_line(tags: SummaryTags) -> str:
    """Render tags as a single line: ``🏷 #тема #Гость #Ведущий #формат #Канал``.

    Порядок логический: тема → гости → ведущие → формат → канал. Пустые поля
    просто пропускаются. Если вообще ни одного тега нет — пустая строка.
    """
    parts: list[str] = []
    if tags.topic:
        parts.append(f"#{tags.topic}")
    for sp in tags.speakers:
        if sp:
            parts.append(f"#{sp}")
    for host in tags.hosts:
        if host:
            parts.append(f"#{host}")
    if tags.format:
        parts.append(f"#{tags.format}")
    if tags.channel:
        parts.append(f"#{tags.channel}")
    if not parts:
        return ""
    # Tags as plain text — Telegram сам делает их кликабельными.
    return "🏷 " + " ".join(parts)
def _build_tags_hints(services: Services) -> tuple[str, str, str]:
    """Build prompt hints for the LLM: existing tags it can reuse.

    Returns ``(topic_hint, speaker_hint, host_hint)``, формат — inline
    предложения, готовые к интерполяции в JSON-schema prompt.
    """
    catalog = services.tags_catalog
    if catalog is None:
        return ("", "", "")
    topics = catalog.all_tags("topic")
    speakers = catalog.all_tags("speaker")
    hosts = catalog.all_tags("host")
    topic_hint = ""
    speaker_hint = ""
    host_hint = ""
    if topics:
        sample = ", ".join(topics[:30])
        topic_hint = f" Уже использованные темы (предпочти их, если подходят): {sample}."
    if speakers:
        sample = ", ".join(speakers[:30])
        speaker_hint = f" Уже использованные фамилии гостей (предпочти их, если подходят): {sample}."
    if hosts:
        sample = ", ".join(hosts[:30])
        host_hint = f" Уже использованные фамилии ведущих (предпочти их, если подходят): {sample}."
    return (topic_hint, speaker_hint, host_hint)
def _resolve_summary_tags(
    *,
    raw_tags: SummaryTags,
    channel_name: str,
    services: Services,
) -> SummaryTags:
    """Take raw LLM tags + channel from metadata, produce canonical SummaryTags."""
    catalog = services.tags_catalog
    if catalog is None:
        # Без каталога просто возвращаем то, что пришло, плюс канал.
        channel_tag = _normalize_channel_simple(channel_name)
        return dataclasses.replace(raw_tags, channel=channel_tag)

    topic = catalog.lookup_or_add("topic", raw_tags.topic) or ""
    canonical_speakers = _canonicalize_names(catalog, "speaker", raw_tags.speakers, limit=3)
    canonical_hosts = _canonicalize_names(catalog, "host", raw_tags.hosts, limit=5)
    fmt = catalog.lookup_or_add("format", raw_tags.format) or ""
    channel = catalog.lookup_or_add("channel", channel_name) or ""

    return SummaryTags(
        topic=topic,
        speakers=tuple(canonical_speakers),
        hosts=tuple(canonical_hosts),
        format=fmt,
        channel=channel,
    )
def _canonicalize_names(
    catalog: TagsCatalog, category: str, raw: tuple[str, ...] | list[str], *, limit: int,
) -> list[str]:
    """Прогнать каждое имя через catalog.lookup_or_add, дропать дубликаты."""
    out: list[str] = []
    seen: set[str] = set()
    for name in list(raw)[:limit]:
        canon = catalog.lookup_or_add(category, name)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out
def _normalize_channel_simple(channel_name: str) -> str:
    """Fallback нормализация имени канала, если каталога нет."""
    s = (channel_name or "").strip()
    if not s:
        return ""
    s = "_".join(s.split())
    return s[:1].upper() + s[1:]
# Пары открывающих/закрывающих кавычек по локали (см. summary.top_comment):
# ru/ar/fa используют «елочки», остальные локали — типографские “ ”.
_QUOTE_PAIRS: dict[str, str] = {"«": "»", "“": "”", '"': '"'}


def _format_top_comment_line(top_comment: VideoComment, available_chars: int, lang: str = "ru") -> str:
    if available_chars <= 0:
        return ""

    likes_label = _format_likes(top_comment.like_count, lang)
    prefix = t("summary.top_comment", lang, likes=likes_label)
    open_quote = prefix[-1] if prefix else "«"
    close_quote = _QUOTE_PAIRS.get(open_quote, "»")
    suffix = f"{close_quote}</i>"
    max_body_chars = min(TOP_COMMENT_MAX_CHARS, max(0, available_chars - len(prefix) - len(suffix)))
    if max_body_chars <= 0:
        return ""

    raw_text = top_comment.text.strip()
    snippet = _fit_escaped_text(raw_text, max_body_chars)
    return f"{prefix}{snippet}{suffix}"
def _fit_escaped_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(escape_html(text)) <= max_chars:
        return escape_html(text)

    ellipsis = "..."
    low = 0
    high = len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + ellipsis
        escaped = escape_html(candidate)
        if len(escaped) <= max_chars:
            best = escaped
            low = mid + 1
        else:
            high = mid - 1
    return best or ellipsis[:max_chars]
def _format_likes(count: int, lang: str = "ru") -> str:
    """Render like count: ru — proper declension '1 лайк' / '2 лайка' / '5 лайков',
    other languages — 'summary.likes' locale key ('{count} likes').

    Once we cross a thousand, we collapse the number to a compact ``1.2к`` /
    ``12к`` form because (a) it's easier on the eye in chat, and (b) once the
    counter is big the exact number stops being interesting. Same pattern as
    ``_format_elapsed_minutes``: ru keeps its declension logic verbatim.
    """
    if lang != "ru":
        if count == 1:
            return t("summary.likes_one", lang, count=count)
        label = _format_compact_count(count, lang) if count >= 1000 else str(count)
        return t("summary.likes", lang, count=label)
    if count >= 1000:
        return f"{_format_compact_count(count)} лайков"
    last_two = count % 100
    last = count % 10
    if 11 <= last_two <= 14:
        word = "лайков"
    elif last == 1:
        word = "лайк"
    elif 2 <= last <= 4:
        word = "лайка"
    else:
        word = "лайков"
    return f"{count} {word}"
def _format_compact_count(count: int, lang: str = "ru") -> str:
    """Compact thousands/millions: 1500 → '1.5к', 12500 → '12к', 1_500_000 → '1.5м'.

    Суффикс локализуется минимально: кириллические «к»/«м» только для ru,
    остальные языки получают латинские K/M (общепринятые и для fa/ar/id,
    где числа по Global Constraints — западные).
    """
    thousands_suffix, millions_suffix = ("к", "м") if lang == "ru" else ("K", "M")
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        if count < 10_000:
            value = round(count / 1000, 1)
            return f"{value:g}{thousands_suffix}"  # 1к, 1.2к, 9.9к
        return f"{count // 1000}{thousands_suffix}"
    value = round(count / 1_000_000, 1)
    return f"{value:g}{millions_suffix}"
async def _send_summary_delivery(
    services: Services,
    job: SummaryJob,
    text: str,
    video_id: str | None = None,
    telegraph_url: str | None = None,
) -> None:
    """Send the final summary message to the user.

    Manual jobs reply to the original message. Scheduled jobs go through bot.send_message
    with disable_notification=True so the user isn't pinged overnight.

    Attaches an inline keyboard:
      • «📄 Транскрипт (md)» + «Скачать аудио» (owner-only) — top row,
      • «подробное саммари» → Telegra.ph URL — full-width bottom row.
    """
    reply_markup = _build_summary_keyboard(
        telegraph_url=telegraph_url,
        video_id=video_id,
        is_owner=_job_is_owner(job, services),
        lang=job.lang,
    )
    if job.message is not None and not job.scheduled:
        await job.message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        return

    if services.bot is None:
        logger.warning(
            "delivery.bot_missing sequence=%s chat_id=%s",
            job.sequence,
            job.chat_id,
        )
        return

    await services.bot.send_message(
        chat_id=job.chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        disable_notification=job.disable_notification,
        reply_markup=reply_markup,
    )
def _build_summary_keyboard(
    *,
    telegraph_url: str | None,
    video_id: str | None,
    is_owner: bool,
    lang: str = "ru",
) -> InlineKeyboardMarkup | None:
    """Собрать inline-клавиатуру под финальным саммари.

    Ряд 1 (верхний, в одну строку) — если есть video_id:
      1. «📄 Транскрипт (md)» — callback, доступ проверяется в хендлере
         (allowlist и подписчики); видна всем, чтобы не выдавать статус
         подписки видимостью кнопки.
      2. «Скачать аудио» — owner-only, callback на транскрипцию/отправку файла.
         Owner-поверхность — не локализуется.
    Ряд 2 (нижний, во всю ширину) — если есть telegraph_url:
      3. «подробное саммари» — ссылка на Telegra.ph. Видна всем.

    Возвращаем None, если ни одна кнопка не применима (нет telegraph_url и
    нет video_id) — тогда саммари уходит вообще без клавиатуры.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if video_id:
        row: list[InlineKeyboardButton] = [
            InlineKeyboardButton(
                text=t("btn.transcript", lang),
                callback_data=f"transcript:{video_id}",
            )
        ]
        if is_owner:
            row.append(
                InlineKeyboardButton(
                    text="Скачать аудио",
                    callback_data=f"download:{video_id}",
                )
            )
        rows.append(row)
        if is_owner:
            # Реф-шеринг, ступень 0: кнопка видна только владельцу
            # (owner-поверхность — не локализуется).
            rows.append([
                InlineKeyboardButton(
                    text="📤 Поделиться",
                    callback_data=f"share:{video_id}",
                )
            ])
    if telegraph_url:
        rows.append([
            InlineKeyboardButton(text=t("btn.details", lang), url=telegraph_url)
        ])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)
def _is_job_cacheable(job: SummaryJob) -> bool:
    """We cache only canonical full-video summaries.

    Segment-mode (scheduled jobs that hit `expert_segment_threshold_sec` and
    produced span-filtered transcripts) gets a summary specific to one
    expert window — it would be wrong to serve that as the canonical answer
    when a later user requests the *whole* video.
    """
    return not (job.segment_spans and len(job.segment_spans) > 0)
def _lookup_cached_summary(url: str, services: Services, lang: str = "ru") -> CachedSummary | None:
    """Resolve URL → video_id → cached entry for ``lang``, swallowing parse errors."""
    if services.summary_cache is None:
        return None
    try:
        video_id = extract_video_id(url)
    except Exception:
        return None
    return services.summary_cache.get(video_id, lang=lang)
def _format_cached_summary_text(
    cached: CachedSummary,
    override_top_comments: list[VideoComment] | None = None,
    bot_username: str | None = None,
    lang: str = "ru",
) -> str:
    """Render the cached summary identically to a fresh delivery — no header
    or "this is cached" marker. From the user's perspective, sending a link a
    second time should feel like a normal, fast response. Cache-hits are still
    visible in logs (``queue.cache.hit`` / ``job.cache.hit``) for diagnostics.

    ``override_top_comments`` лет сделать так: «выдать саммари с комментами,
    обновлёнными прямо сейчас», не меняя сам ``cached`` объект.
    """
    summary = cached.to_summary()
    if override_top_comments is not None:
        comments = override_top_comments
    else:
        comments = cached.to_top_comments()
    return _format_telegram_summary(
        title=cached.title,
        video_url=cached.url,
        summary=summary,
        telegraph_url=cached.telegraph_url,
        channel_name=cached.channel_name,
        channel_url=cached.channel_url,
        top_comment=comments[0] if comments else None,
        bot_username=bot_username,
        lang=lang,
    )
async def _refresh_cached_comments(
    cached: CachedSummary, services: Services, source_label: str, lang: str = "ru"
) -> list[VideoComment]:
    """Get fresh top-comments and, if they changed since the cache was made,
    rewrite the cached entry + edit the existing Telegra.ph page so all
    surfaces (Telegram, Telegraph) stay in sync.

    Returns the list of comments to actually display. Falls back to the cached
    ones on any failure — comments-refresh is opportunistic, not critical.
    """
    try:
        fresh = await asyncio.to_thread(services.youtube.fetch_top_comments, cached.url)
    except Exception:
        logger.exception(
            "cache.refresh_comments.failed source=%s video_id=%s",
            source_label, cached.video_id,
        )
        return cached.to_top_comments()

    cached_comments = cached.to_top_comments()
    if _comments_equivalent(fresh, cached_comments):
        logger.info(
            "cache.refresh_comments.unchanged source=%s video_id=%s count=%s",
            source_label, cached.video_id, len(fresh),
        )
        return cached_comments

    logger.info(
        "cache.refresh_comments.changed source=%s video_id=%s old=%s new=%s",
        source_label, cached.video_id, len(cached_comments), len(fresh),
    )

    # Edit Telegra.ph first. If it succeeds — also update the cache so future
    # cache hits stay consistent with the live page. If it fails (most common
    # cause: ``editPage`` only works with the same access_token that originally
    # created the page; pages from previous bot incarnations are read-only),
    # we still hand the *fresh* comments to the Telegram delivery — that's the
    # surface the user actually sees right now. Cache stays untouched in that
    # case so we don't desync the cache from the read-only Telegraph page.
    telegraph_updated = False
    try:
        await services.telegraph.edit(
            cached.telegraph_url,
            title=cached.title,
            video_url=cached.url,
            summary=cached.to_summary(),
            top_comments=fresh,
            lang=lang,
        )
        telegraph_updated = True
    except Exception:
        logger.exception(
            "cache.refresh_comments.telegraph_edit_failed video_id=%s",
            cached.video_id,
        )

    if telegraph_updated and services.summary_cache is not None:
        cached.top_comments = [
            {
                "text": c.text,
                "author": c.author,
                "like_count": c.like_count,
                "is_pinned": c.is_pinned,
            }
            for c in fresh
        ]
        try:
            services.summary_cache.put(cached, lang=lang)
        except Exception:
            logger.exception(
                "cache.refresh_comments.cache_put_failed video_id=%s",
                cached.video_id,
            )

    # Always return the fresh comments — even if Telegraph couldn't be
    # updated, the user gets actual top-comment in their Telegram message.
    return fresh
def _comments_equivalent(a: list[VideoComment], b: list[VideoComment]) -> bool:
    """True if two top-comment lists describe the same audience reaction.

    Equality based on author + text identity (those don't change). Like counts
    drift constantly; we accept ±10 wobble before deciding the page needs a
    rewrite.
    """
    if len(a) != len(b):
        return False
    for ca, cb in zip(a, b):
        if ca.author != cb.author or ca.text != cb.text:
            return False
        if abs(ca.like_count - cb.like_count) > 10:
            return False
    return True
async def _send_cached_summary_to_chat(
    message: Message,
    cached: CachedSummary,
    services: Services,
) -> None:
    """Manual flow: respond to a user message with cached summary."""
    from app.bot_handlers import _msg_lang  # local: избегаем цикла bot_handlers<->delivery
    lang = _msg_lang(message, services)
    fresh_comments = await _refresh_cached_comments(cached, services, source_label="manual", lang=lang)
    text = _format_cached_summary_text(
        cached, override_top_comments=fresh_comments, bot_username=services.bot_username, lang=lang,
    )
    user_id = _message_user_id(message)
    reply_markup = _build_summary_keyboard(
        telegraph_url=cached.telegraph_url,
        video_id=cached.video_id,
        is_owner=user_id is not None and services.users.is_owner(user_id),
        lang=lang,
    )
    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )
    # NB: исходное user-message с YouTube-ссылкой удаляется централизованно
    # в `_enqueue_summary_job` (finally-блок), как только ссылка попала в
    # обработку. Здесь дублировать delete не нужно.

    # Обновляем pinned digest пользователя — даже на cache-hit это полезно:
    # запись переедет наверх (last-accessed-first), пользователь увидит, что
    # ролик «свежий». Ошибки глушатся внутри хелпера.
    target = _resolve_digest_target(services, message, None)
    if target is not None:
        user_id, digest_chat_id = target
        await _update_user_digest_safely(
            services,
            user_id=user_id,
            chat_id=digest_chat_id,
            video_id=cached.video_id,
            title=cached.title,
            telegraph_url=cached.telegraph_url,
            channel_name=cached.channel_name or "",
            created_at_unix=cached.created_at_unix or time.time(),
        )
async def _send_quota_denied(message: Message, services: Services, verdict, lang: str = "ru") -> None:
    """Отказ по квоте + кнопка оформления подписки.

    callback 'subscribe' обрабатывается в bot_handlers (шлёт Stars-инвойс) —
    кнопка работает и из этого сообщения, и из /subscribe.
    """
    user_id = _message_user_id(message)
    if services.analytics is not None and user_id is not None:
        services.analytics.record(user_id, "quota_denied", detail=verdict.deny_reason)
    s = services.settings
    if verdict.deny_reason == "monthly_exhausted":
        text = t("quota.denied.monthly", lang, monthly=s.quota_sub_monthly)
        await message.answer(text)
        return
    text = t(
        "quota.denied.weekly", lang,
        price=s.subscription_price_stars, monthly=s.quota_sub_monthly,
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=t("quota.subscribe_button", lang, price=s.subscription_price_stars),
            callback_data="subscribe",
        )
    ]])
    await message.answer(text, reply_markup=keyboard)
async def _deliver_cached_summary_for_job(
    job: SummaryJob,
    services: Services,
    cached: CachedSummary,
) -> None:
    """Job-level delivery: works for both manual (job.message != None) and
    scheduled (services.bot.send_message)."""
    if job.scheduled and services.morning_digest is not None:
        # Cache-hit на scheduled-job — тоже не шлём отдельным сообщением,
        # кладём в тот же дайджест-батч, что и свежесгенерированные саммари.
        services.morning_digest.add(MorningDigestItem(
            video_id=cached.video_id,
            title=cached.title,
            channel_name=cached.channel_name or "",
            telegraph_url=cached.telegraph_url or "",
            overview=cached.summary_overview,
            tags_line=_format_tags_line(cached.tags_obj()),
            duration_sec=0.0,
            created_at_unix=time.time(),
        ))
    else:
        fresh_comments = await _refresh_cached_comments(cached, services, source_label="job", lang=job.lang)
        text = _format_cached_summary_text(
            cached, override_top_comments=fresh_comments, bot_username=services.bot_username,
            lang=job.lang,
        )
        await _send_summary_delivery(
            services=services,
            job=job,
            text=text,
            video_id=cached.video_id,
            telegraph_url=cached.telegraph_url,
        )
    # NB: исходное user-message с ссылкой уже удалено в `_enqueue_summary_job`
    # к моменту, когда job попал в обработку. У scheduled-job его и не было.

    # Обновляем pinned digest. Для scheduled-job (monitoring) кладём в owner.
    target = _resolve_digest_target(services, job.message, job)
    if target is not None:
        user_id, digest_chat_id = target
        await _update_user_digest_safely(
            services,
            user_id=user_id,
            chat_id=digest_chat_id,
            video_id=cached.video_id,
            title=cached.title,
            telegraph_url=cached.telegraph_url,
            channel_name=cached.channel_name or "",
            created_at_unix=cached.created_at_unix or time.time(),
        )
def _save_summary_to_cache(
    *,
    services: Services,
    video_id: str,
    url: str,
    title: str,
    metadata,
    summary,
    telegraph_url: str,
    transcript_source: str,
    transcript_chars: int,
    model: str,
    top_comments: list[VideoComment] | None = None,
    lang: str = "ru",
) -> None:
    """Persist a freshly-generated summary so future requests for the same
    (video_id, lang) are answered from cache."""
    if services.summary_cache is None:
        return
    now = time.time()
    iso_time = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat()
    entry = CachedSummary(
        video_id=video_id,
        url=url,
        title=title,
        channel_name=getattr(metadata, "channel_name", "") or "",
        channel_url=getattr(metadata, "channel_url", "") or "",
        summary_overview=summary.overview,
        summary_key_points=list(summary.key_points),
        summary_chapters=[
            {"start": ch.start, "title": ch.title, "notes": ch.notes}
            for ch in summary.chapters
        ],
        summary_raw_text=summary.raw_text,
        telegraph_url=telegraph_url,
        transcript_url=None,
        transcript_source=transcript_source,
        model=model or "unknown",
        created_at_iso=iso_time,
        created_at_unix=now,
        transcript_chars=transcript_chars,
        top_comments=[
            {
                "text": c.text,
                "author": c.author,
                "like_count": c.like_count,
                "is_pinned": c.is_pinned,
            }
            for c in (top_comments or [])
        ],
        tag_topic=summary.tags.topic,
        tag_speakers=list(summary.tags.speakers),
        tag_hosts=list(summary.tags.hosts),
        tag_format=summary.tags.format,
        tag_channel=summary.tags.channel,
    )
    services.summary_cache.put(entry, lang=lang)
def _format_generation_error(video_url: str, title: str, reason: str, lang: str = "ru") -> str:
    label = title.strip() or video_url
    if video_url:
        link = f'<a href="{escape_html(video_url)}">{escape_html(label)}</a>'
    else:
        link = escape_html(label)
    reason_text = reason.strip() or t("error.unknown_reason", lang)
    return t(
        "error.generation_failed", lang,
        link=link, reason=escape_html(reason_text),
    )[:4000]
# Тизер шер-сообщения: первые предложения overview, не больше этого объёма.
_SHARE_OVERVIEW_MAX_CHARS = 300


def _first_sentences(text: str, max_chars: int) -> str:
    """Первые предложения текста до max_chars, обрезка по границе предложения."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for stop in (". ", "! ", "? "):
        idx = cut.rfind(stop)
        if idx > 40:
            return cut[: idx + 1].strip()
    return cut.rstrip() + "…"


def build_share_message(cached: CachedSummary, bot_username: str, referrer_id: int) -> str:
    """Форвардабельное шер-сообщение: польза чату + реф-ссылка текстом.

    Без inline-кнопок: Telegram срезает их при форварде, а ссылка текстом
    переживает и форвард, и копипаст (спека реф-шеринга, ступень 0).
    """
    ref_link = f"https://t.me/{bot_username}?start=r{referrer_id}_{cached.video_id}"
    title = escape_html(cached.title)
    channel = escape_html(cached.channel_name or "")
    teaser = escape_html(_first_sentences(cached.summary_overview, _SHARE_OVERVIEW_MAX_CHARS))
    minutes = _estimate_reading_time_minutes(cached.to_summary())
    lines = [f"🎬 <b>{title}</b>"]
    if channel:
        lines.append(channel)
    lines.append("")
    lines.append(teaser)
    lines.append("")
    lines.append(
        f'🔮 <a href="{ref_link}">Полное саммари у бота</a> — {minutes} мин чтения'
    )
    return "\n".join(lines)


def _estimate_reading_time_minutes(summary: Summary) -> int:
    parts: list[str] = [summary.overview]
    parts.extend(summary.key_points)
    for chapter in summary.chapters:
        parts.append(chapter.title)
        parts.append(chapter.notes)
    words = sum(len(part.split()) for part in parts if part)
    return max(1, round(words / 180))
def _resolve_digest_target(
    services: Services,
    message: Message | None,
    job: SummaryJob | None = None,
) -> tuple[int, int] | None:
    """Return (user_id, chat_id) for pinning a digest, or None to skip.

    Manual flow → берём from_user.id и message.chat.id.
    Scheduled flow (job.message is None / from monitoring) → owner_user_id
    в его private-чате с ботом (chat_id == owner_user_id). Если owner не
    настроен — пропускаем (некому показывать).
    """
    if message is not None and message.from_user is not None:
        return message.from_user.id, message.chat.id
    owner = services.settings.owner_user_id
    if owner is None:
        return None
    return owner, owner
async def _update_user_digest_safely(
    services: Services,
    user_id: int,
    chat_id: int,
    *,
    video_id: str,
    title: str,
    telegraph_url: str,
    channel_name: str,
    created_at_unix: float,
) -> None:
    """Fire-and-forget обёртка над update_pin_for_user.

    Никогда не падает — ошибки логирует и проглатывает. Вызывается из путей
    доставки саммари, и обновление дайджеста не должно блокировать или ронять
    доставку. Если digest_store не подключен (`services.digests is None`) или
    бот ещё не инициализирован — тоже тихо выходим.
    """
    digests = services.digests
    bot = services.bot
    if digests is None or bot is None:
        return
    if not telegraph_url:
        # Без Telegra.ph-ссылки тапать в дайджесте будет некуда —
        # такая запись бесполезна. Пропускаем.
        return
    entry = DigestEntry(
        video_id=video_id,
        title=title or video_id,
        telegraph_url=telegraph_url,
        channel_name=channel_name or "",
        created_at_unix=created_at_unix or time.time(),
    )
    try:
        await update_pin_for_user(
            store=digests,
            bot=bot,
            user_id=user_id,
            chat_id=chat_id,
            entry=entry,
        )
    except Exception:
        logger.exception(
            "digests.update_failed user_id=%s chat_id=%s video_id=%s",
            user_id, chat_id, video_id,
        )
def _message_user_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None
def _job_is_owner(job: SummaryJob, services: Services) -> bool:
    if job.message is not None and job.message.from_user is not None:
        return services.users.is_owner(job.message.from_user.id)
    return services.users.is_owner(job.chat_id)
