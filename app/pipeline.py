from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, FSInputFile

from app.groq_whisper_service import GroqWhisperUnavailable
from app.i18n import UserFacingError, t
from app.llm_client import (
    FREE_CHAIN_EXHAUSTED_MARKER,
    OPENROUTER_BUDGET_EXCEEDED_MARKER,
    GenerationUsage,
)
from app.models import VideoComment, VideoMetadata
from app.monitoring_service import filter_segments_by_spans, format_spans_for_humans
from app.morning_digest import MorningDigestItem
from app.summarizer import SummaryProgress
from app.transcript_chunker import chunk_transcript, segments_to_text
from app.transcript_export import save_transcript_markdown
from app.utils import escape_html, extract_video_id
from app.youtube_service import TranscriptUnavailable

from app.services_container import Services, SummaryJob
from app.status_messages import (
    _delete_service_status,
    _estimate_job_total_seconds,
    _forget_service_status,
    _run_with_telegram_status,
    _set_service_status,
)
from app.delivery import (
    _build_tags_hints,
    _deliver_cached_summary_for_job,
    _format_generation_error,
    _format_tags_line,
    _format_telegram_summary,
    _is_job_cacheable,
    _lookup_cached_summary,
    _normalize_channel_simple,
    _resolve_digest_target,
    _resolve_summary_tags,
    _save_summary_to_cache,
    _send_summary_delivery,
    _update_user_digest_safely,
)

# NB: queue_service импортируется ЛОКАЛЬНО внутри функций (_process_youtube_job →
# _enqueue_transcription_job; _process_transcription_job → _summary_queue_worker),
# чтобы разорвать цикл pipeline ↔ queue_service. Направление зависимостей по
# брифу: queue_service зависит от pipeline, не наоборот.


logger = logging.getLogger(__name__)


def _user_facing_error_reason(exc: Exception, job: SummaryJob, services) -> str:
    """Причина ошибки для сообщения пользователю.

    Полный технический текст (HTTP-коды, подсказки /llm_paid, депозит
    OpenRouter) видит ТОЛЬКО владелец — job'ы из его чата. Всем остальным,
    включая allowlist (quota_user_id is None, но чат не владельца),
    технические подробности не показываем: им не помочь советом
    «переключись на платную модель», а сырой текст не локализован.

    Случаи для не-владельца, по убыванию специфичности:
    1. ``UserFacingError`` — текст уже собран через ``t(job.lang)`` выше по
       стеку (например error.heavy_quota) — отдаём как есть, не трогаем.
    2. Известные технические маркеры (free-chain/budget/GROQ_API_KEY) —
       подменяем человеческим объяснением, как раньше.
    3. Всё остальное — необработанное исключение (yt-dlp, LLM-провайдер,
       Telegram и т.п.) — единый error.internal. Полный exc всё равно
       попадает в лог через logger.exception чуть выше по стеку — тут
       ничего не теряем.
    """
    reason = str(exc)
    owner_id = services.settings.owner_user_id if services.settings else None
    if owner_id is not None and job.chat_id == owner_id:
        return reason
    if isinstance(exc, UserFacingError):
        return reason
    if FREE_CHAIN_EXHAUSTED_MARKER in reason:
        # allowlist (quota_user_id is None) и подписчики — без питча /subscribe:
        # первым подписка не нужна, у вторых она уже есть.
        is_sub = bool(
            job.quota_user_id is not None
            and services.billing is not None
            and services.billing.is_subscriber(job.quota_user_id)
        )
        if is_sub or job.quota_user_id is None:
            return t("error.temporary_overload", job.lang)
        return t("error.daily_free_limit", job.lang)
    if OPENROUTER_BUDGET_EXCEEDED_MARKER in reason:
        return t("error.service_budget", job.lang)
    if "GROQ_API_KEY" in reason:
        return t("error.groq_unavailable", job.lang, error="GROQ_API_KEY not configured")
    return t("error.internal", job.lang)


def _llm_route_for_job(job: SummaryJob, services: Services) -> str:
    """Маршрут LLM по тарифу пользователя (см. README, «Маршрутизация LLM»).

    NB: route и списание (charge) читают статус подписки в разные моменты:
    route — перед генерацией, kind списания — после доставки. При истечении
    подписки посреди job'а расхождение в пользу пользователя (получил
    paid_fallback, списали как free) — осознанная семантика.
    """
    if job.quota_user_id is None:
        return "default"
    is_sub = bool(
        services.billing is not None
        and services.billing.is_subscriber(job.quota_user_id)
    )
    return "paid_fallback" if is_sub else "free_only"


def _is_upcoming(metadata: VideoMetadata) -> bool:
    """Премьера или запланированный стрим, у которого контента ещё нет."""
    if metadata.live_status == "is_upcoming":
        return True
    ts = metadata.release_timestamp
    return bool(ts and ts > time.time())


def _format_local_time(services: Services, ts: float) -> str:
    """Unix-время → «04.07 18:00» в таймзоне бота (scan_tz мониторинга)."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    tz_name = "Europe/Moscow"
    if services.monitoring is not None and services.monitoring.rules.scan_tz:
        tz_name = services.monitoring.rules.scan_tz
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = _dt.timezone.utc
    return _dt.datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")


async def _defer_premiere_job(
    job: SummaryJob,
    services: Services,
    metadata: VideoMetadata,
    job_id: str,
) -> None:
    """Отложить job премьеры до release + PREMIERE_SUMMARY_DELAY_HOURS.

    Строка в jobs переводится в статус "deferred" с run_after; поднимет её
    deferred-scheduler (queue_service.run_deferred_jobs_scheduler). Если
    время выхода неизвестно (yt-dlp не отдал release_timestamp) — пробуем
    через delay + 1 час от текущего момента.
    """
    delay_sec = services.settings.premiere_delay_hours * 3600
    release_ts = metadata.release_timestamp
    run_after = (release_ts + delay_sec) if release_ts else (time.time() + delay_sec + 3600)

    title_link = f'<a href="{escape_html(job.url)}">{escape_html(metadata.title)}</a>'
    if services.job_store is None or job.db_id is None:
        # Отложить не через что (нет персистентной строки) — честно просим
        # вернуться позже; job завершается без ошибки.
        await _send_summary_delivery(
            services=services,
            job=job,
            text=t("premiere.no_store", job.lang, title_link=title_link),
        )
        await _delete_service_status(services, job.chat_id)
        return

    job.deferred_until = run_after
    services.job_store.set_deferred(job.db_id, run_after)
    logger.info(
        "job.premiere.deferred job_id=%s video_id=%s release_ts=%s run_after=%.0f",
        job_id,
        metadata.video_id,
        f"{release_ts:.0f}" if release_ts else "unknown",
        run_after,
    )

    if release_ts:
        text = t(
            "premiere.deferred", job.lang,
            title_link=title_link,
            release=_format_local_time(services, release_ts),
            return_time=_format_local_time(services, run_after),
            hours=services.settings.premiere_delay_hours,
        )
    else:
        text = t(
            "premiere.unknown_release", job.lang,
            title_link=title_link,
            return_time=_format_local_time(services, run_after),
        )
    await _send_summary_delivery(services=services, job=job, text=text)
    await _delete_service_status(services, job.chat_id)


async def _process_youtube_job(job: SummaryJob, services: Services) -> None:
    from app.queue_service import _enqueue_transcription_job  # local: avoid pipeline<->queue_service cycle
    job_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    message = job.message
    url = job.url
    chat_id = job.chat_id
    video_id = "unknown"
    transcript_source = "unknown"
    title = url

    # Cache check at the very top — covers scheduled jobs + race-conditions
    # where the same video was queued twice in quick succession.
    if _is_job_cacheable(job):
        cached = _lookup_cached_summary(url, services, lang=job.lang)
        if cached is not None:
            logger.info(
                "job.cache.hit job_id=%s chat_id=%s video_id=%s telegraph_url=%s",
                job_id, chat_id, cached.video_id, cached.telegraph_url,
            )
            await _deliver_cached_summary_for_job(job, services, cached)
            return

    await _set_service_status(services, message, t("status.fetching", job.lang), job=job)
    try:
        video_id = extract_video_id(url)
        logger.info(
            "job.start job_id=%s chat_id=%s video_id=%s url=%s scheduled=%s",
            job_id,
            chat_id,
            video_id,
            url,
            job.scheduled,
        )

        stage_started = time.monotonic()
        if job.pre_fetched_metadata is not None:
            metadata = job.pre_fetched_metadata
            logger.info(
                "job.metadata.reused job_id=%s video_id=%s title=%r",
                job_id,
                metadata.video_id,
                metadata.title,
            )
        else:
            metadata = await asyncio.to_thread(services.youtube.fetch_metadata, url)
            logger.info(
                "job.metadata.done job_id=%s video_id=%s title=%r channel=%r duration_sec=%.1f",
                job_id,
                metadata.video_id,
                metadata.title,
                metadata.channel_name,
                time.monotonic() - stage_started,
            )
        video_id = metadata.video_id
        title = metadata.title
        job.video_duration_sec = metadata.duration_sec or None

        # Премьера / запланированный стрим: контента ещё нет. Запоминаем
        # время выхода и откладываем job — deferred-scheduler вернёт его
        # в очередь через PREMIERE_SUMMARY_DELAY_HOURS после релиза.
        if _is_upcoming(metadata):
            await _defer_premiere_job(job, services, metadata, job_id)
            return

        if job.pre_fetched_segments is not None:
            segments = list(job.pre_fetched_segments)
            transcript_source = job.pre_fetched_transcript_source or "youtube"
            logger.info(
                "job.transcript.reused job_id=%s source=%s segments=%s",
                job_id,
                transcript_source,
                len(segments),
            )
        else:
            try:
                await _set_service_status(services, message, t("status.captions", job.lang), job=job)
                stage_started = time.monotonic()
                segments = await asyncio.to_thread(services.youtube.fetch_transcript, video_id)
                transcript_source = "youtube"
                logger.info(
                    "job.transcript.done job_id=%s source=youtube segments=%s duration_sec=%.1f",
                    job_id,
                    len(segments),
                    time.monotonic() - stage_started,
                )
            except TranscriptUnavailable:
                if services.groq_whisper is None or not services.groq_whisper.enabled:
                    raise RuntimeError(
                        "Субтитры YouTube недоступны для этого ролика, "
                        "а GROQ_API_KEY не настроен — облачное распознавание "
                        "выключено. Добавь ключ Groq в .env и перезапусти бот."
                    )
                logger.info(
                    "job.transcript.unavailable job_id=%s fallback=groq_queue",
                    job_id,
                )
                # Тяжёлый ролик для платного пользователя: Groq-транскрипция
                # длинного видео стоит на порядок дороже — списываем 2 единицы.
                if (
                    job.quota_user_id is not None
                    and services.quota is not None
                    and (metadata.duration_sec or 0) >= services.settings.heavy_duration_sec
                ):
                    verdict = services.quota.check(job.quota_user_id, weight=2)
                    if not verdict.allowed:
                        raise UserFacingError(
                            t("error.heavy_quota", job.lang, remaining=verdict.remaining)
                        )
                    job.usage_weight = 2
                    await _set_service_status(
                        services, message,
                        t("status.heavy_charge", job.lang),
                        job=job,
                    )
                await _set_service_status(
                    services,
                    message,
                    t("status.transcribe_queue", job.lang),
                    job=job,
                )
                job.routed_to_transcription = True
                await _enqueue_transcription_job(job, services)
                logger.info(
                    "job.routed_to_transcription job_id=%s url=%s sequence=%s",
                    job_id,
                    url,
                    job.sequence,
                )
                # Возвращаемся: main worker возьмёт следующий job из summary_queue,
                # а transcription worker сам перенаправит этот job обратно после
                # успешного распознавания. Статус в БД остаётся "active" —
                # см. SummaryJob.routed_to_transcription.
                return

        # If this is a scheduled segment-mode job, trim segments to the expert span(s).
        if job.segment_spans:
            original_count = len(segments)
            segments = filter_segments_by_spans(segments, job.segment_spans)
            logger.info(
                "job.segment_filter.done job_id=%s spans=%s before=%s after=%s",
                job_id,
                job.segment_spans,
                original_count,
                len(segments),
            )

        transcript_text = segments_to_text(segments)

        # Сохраняем транскрипт в markdown — его можно скачать кнопкой под
        # саммари (allowlist и подписчики). Ошибка записи не ломает job.
        # Segment-mode (job.segment_spans) пишет лишь фрагмент ролика —
        # сохранять его как канонический транскрипт video_id нельзя
        # (аналогично _is_job_cacheable для кэша саммари).
        if not job.segment_spans:
            try:
                saved = await asyncio.to_thread(
                    save_transcript_markdown,
                    services.settings.bot_data_dir, video_id, title, url, segments,
                )
                logger.info("job.transcript_md.saved job_id=%s path=%s", job_id, saved)
            except Exception as exc:  # noqa: BLE001
                logger.warning("job.transcript_md.save_failed job_id=%s error=%s", job_id, exc)

        active_model = await services.llm.active_model()
        chunk_size = services.settings.effective_chunk_max_chars(active_model=active_model)
        chunks = chunk_transcript(transcript_text, max_chars=chunk_size)
        job.progress_estimate_sec = _estimate_job_total_seconds(
            transcript_chars=len(transcript_text),
            chunks_count=len(chunks),
            transcript_source=transcript_source,
            llm_provider=services.settings.llm_provider,
        )
        logger.info(
            "job.chunking.done job_id=%s transcript_chars=%s chunks=%s max_chars=%s "
            "provider=%s active_model=%s",
            job_id,
            len(transcript_text),
            len(chunks),
            chunk_size,
            services.settings.llm_provider,
            active_model,
        )
        logger.info(
            "job.progress_estimate.done job_id=%s estimate_sec=%.1f",
            job_id,
            job.progress_estimate_sec or 0.0,
        )

        # Тянем топ-комментарии параллельно с генерацией саммари. yt-dlp на
        # comments-extractor'е тратит 5–15 секунд, и если запускать после LLM,
        # это всё уходит в общий duration. Параллельный запуск экономит ровно
        # это время — к моменту, когда саммари готов, комменты обычно уже на
        # руках. Failure-mode тот же: ошибка/отключённые → пустой список.
        comments_task = asyncio.create_task(_fetch_top_comments_background(services, url, job_id))

        usage = GenerationUsage()
        summary_progress = SummaryProgress()
        context_hint = _build_context_hint(job)
        topic_hint, speaker_hint, host_hint = _build_tags_hints(services)

        # Tier-маршрутизация LLM: подписчик получает paid-fallback (free
        # сначала, платная при недоступности/медленности); бесплатный внешний
        # пользователь никогда не тратит платные токены; allowlist — как
        # раньше, по глобальному /llm_paid.
        llm_route = _llm_route_for_job(job, services)
        if job.quota_user_id is not None:
            logger.info("job.llm_route job_id=%s route=%s", job_id, llm_route)

        generating_text = t("status.generating", job.lang, provider=services.llm.provider_name)
        await _set_service_status(services, message, generating_text, job=job)
        summary = await _run_with_telegram_status(
            services=services,
            source_message=message,
            operation=services.summarizer.summarize(
                url=url,
                title=title,
                chunks=chunks,
                progress=summary_progress,
                usage=usage,
                context_hint=context_hint,
                topic_hint=topic_hint,
                speaker_hint=speaker_hint,
                host_hint=host_hint,
                llm_route=llm_route,
                output_lang=job.lang,
            ),
            base_text=generating_text,
            job=job,
        )

        # Нормализуем теги через TagsCatalog (fuzzy match на каталог) и
        # добавляем тег канала из metadata. Каталог хранит русские
        # темы/фамилии/форматы — прогонять через него теги не-ru саммари
        # нельзя (LLM и так вернул их на языке саммари). Для не-ru оставляем
        # raw-теги LLM как есть, канонизируя только имя канала.
        if job.lang == "ru":
            summary = dataclasses.replace(
                summary,
                tags=_resolve_summary_tags(
                    raw_tags=summary.tags,
                    channel_name=getattr(metadata, "channel_name", "") or "",
                    services=services,
                ),
            )
        else:
            summary = dataclasses.replace(
                summary,
                tags=dataclasses.replace(
                    summary.tags,
                    channel=_normalize_channel_simple(getattr(metadata, "channel_name", "") or ""),
                ),
            )

        if not comments_task.done():
            await _set_service_status(
                services, message, t("status.waiting_comments", job.lang), job=job
            )
        try:
            top_comments = await comments_task
        except Exception as exc:  # noqa: BLE001
            # Не ERROR: для роликов без комментариев / с отключёнными комментами
            # это ожидаемый кейс. Идём дальше без top-комментария.
            logger.warning("job.comments.await_failed job_id=%s error=%s", job_id, exc)
            top_comments = []

        publishing_text = t("status.publishing", job.lang)
        await _set_service_status(services, message, publishing_text, job=job)
        try:
            telegraph_url = await _run_with_telegram_status(
                services=services,
                source_message=message,
                operation=services.telegraph.publish(
                    title=title,
                    url=url,
                    summary=summary,
                    top_comments=top_comments,
                    lang=job.lang,
                ),
                base_text=publishing_text,
                job=job,
            )
        except Exception:
            # Telegra.ph лежит — не роняем job: пользователь получит краткое
            # саммари в чат, просто без кнопки на полный конспект. Кэш не
            # пишем (без URL запись бесполезна), дайджест сам пропустит
            # запись без telegraph_url.
            logger.exception("job.telegraph.publish_failed job_id=%s — деградируем без страницы", job_id)
            telegraph_url = ""

        total_duration_sec = time.monotonic() - started
        # model_name всё ещё нужен ниже — пишем в кэш (CachedSummary.model) и
        # в строку job.done для аналитики. Сам user-facing «service info»-блок
        # (Модель/Контекст/Температура/Токены/Источник transcript/…) убран —
        # owner получает то же чистое саммари, что и обычные пользователи.
        try:
            model_name = await services.llm.active_model()
        except Exception as exc:
            logger.warning("service_info.model_lookup_failed job_id=%s error=%s", job_id, exc)
            model_name = "unknown"

        if job.scheduled and services.morning_digest is not None:
            # Scheduled-саммари не шлём отдельным сообщением — оно уйдёт
            # одной строкой утреннего дайджеста после разбора всей пачки.
            services.morning_digest.add(MorningDigestItem(
                video_id=video_id,
                title=title,
                channel_name=getattr(metadata, "channel_name", "") or "",
                telegraph_url=telegraph_url or "",
                overview=summary.overview,
                tags_line=_format_tags_line(summary.tags),
                duration_sec=metadata.duration_sec or 0.0,
                created_at_unix=time.time(),
            ))
        else:
            summary_text = _format_telegram_summary(
                title=title,
                video_url=url,
                summary=summary,
                telegraph_url=telegraph_url,
                channel_name=metadata.channel_name,
                channel_url=metadata.channel_url,
                scheduled=job.scheduled,
                segment_spans=job.segment_spans,
                expert_matches=job.expert_matches,
                top_comment=top_comments[0] if top_comments else None,
                bot_username=services.bot_username,
                lang=job.lang,
            )
            await _send_summary_delivery(
                services=services,
                job=job,
                text=summary_text,
                video_id=video_id,
                telegraph_url=telegraph_url or None,
            )
        # Сервисное сообщение со статусом («Получаю данные…», «Генерирую
        # summary…» и т.п.) дослужило — удаляем его, чтобы в чате осталось
        # только финальное саммари.
        await _delete_service_status(services, chat_id)

        # Обновляем закреплённый дайджест последних саммари у пользователя
        # (или у owner'а — для scheduled-job'ов из monitoring). Ошибки тут
        # глушатся внутри хелпера — доставка саммари важнее, чем закреп.
        target = _resolve_digest_target(services, message, job)
        if target is not None:
            user_id, digest_chat_id = target
            await _update_user_digest_safely(
                services,
                user_id=user_id,
                chat_id=digest_chat_id,
                video_id=video_id,
                title=title,
                telegraph_url=telegraph_url,
                channel_name=getattr(metadata, "channel_name", "") or "",
                created_at_unix=time.time(),
            )
        # NB: исходное user-message с YouTube-ссылкой удалили ещё на этапе
        # `_enqueue_summary_job` (finally-блок), как только ссылка попала
        # в очередь. Здесь ничего удалять не нужно.

        # Списываем квоту только после успешной доставки. Кэш-хиты сюда не
        # доходят (fast-path выше), упавшие job'ы — тоже (except-ветка).
        if job.quota_user_id is not None and services.quota is not None:
            try:
                services.quota.charge(job.quota_user_id, video_id, job.usage_weight)
            except Exception:
                logger.exception("billing.charge_failed user_id=%s", job.quota_user_id)

        # Кэшируем результат — но только для full-video. Segment-mode даёт
        # частичное саммари по конкретному эксперту, его нельзя считать
        # «каноном» для этого video_id.
        if (
            _is_job_cacheable(job)
            and services.summary_cache is not None
            and video_id != "unknown"
            and telegraph_url
        ):
            try:
                _save_summary_to_cache(
                    services=services,
                    video_id=video_id,
                    url=url,
                    title=title,
                    metadata=metadata,
                    summary=summary,
                    telegraph_url=telegraph_url,
                    transcript_source=transcript_source,
                    transcript_chars=len(transcript_text),
                    model=model_name,
                    top_comments=top_comments,
                    lang=job.lang,
                )
            except Exception:
                logger.exception("job.cache.save_failed job_id=%s video_id=%s", job_id, video_id)

        logger.info(
            "job.done job_id=%s video_id=%s duration_sec=%.1f telegraph_url=%s "
            "model=%s "
            "llm_calls=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s llm_sec=%.1f",
            job_id,
            video_id,
            total_duration_sec,
            telegraph_url,
            model_name,
            usage.calls,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.duration_sec,
        )
    except asyncio.CancelledError:
        logger.info("job.cancelled job_id=%s video_id=%s duration_sec=%.1f", job_id, video_id, time.monotonic() - started)
        try:
            await _set_service_status(services, message, t("status.stopped", job.lang), job=job)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("progress.edit.failed error=%s", exc)
        _forget_service_status(services, chat_id)
        raise
    except Exception as exc:
        logger.exception("job.failed job_id=%s video_id=%s duration_sec=%.1f", job_id, video_id, time.monotonic() - started)
        await _set_service_status(services, message, t("status.interrupted", job.lang), job=job)
        await _send_summary_delivery(
            services=services,
            job=job,
            text=_format_generation_error(
                video_url=url,
                title=title,
                reason=_user_facing_error_reason(exc, job, services),
                lang=job.lang,
            ),
        )
        _forget_service_status(services, chat_id)
        raise


async def _fetch_top_comments_background(
    services: Services,
    url: str,
    job_id: str,
) -> list[VideoComment]:
    """Background-friendly wrapper around YouTubeService.fetch_top_comments.

    Same failure semantics as the inline version: log + return empty list,
    so that parallel summary generation never breaks because of comments.
    """
    try:
        comments = await asyncio.to_thread(services.youtube.fetch_top_comments, url)
        logger.info("job.comments.done job_id=%s count=%s", job_id, len(comments))
        return comments
    except asyncio.CancelledError:
        logger.info("job.comments.cancelled job_id=%s", job_id)
        raise
    except Exception as exc:  # noqa: BLE001
        # Не ERROR: yt-dlp на comments-extractor'е регулярно падает на роликах
        # с отключёнными / залоченными комментами. Это ожидаемый failure-mode,
        # пользователю он не виден (саммари всё равно публикуется), так что
        # пусть будет WARNING, чтобы не портить error-counter в /stats.
        logger.warning("job.comments.failed job_id=%s error=%s", job_id, exc)
        return []


async def _process_transcription_job(job: SummaryJob, services: Services) -> None:
    from app.queue_service import _summary_queue_worker  # local: avoid pipeline<->queue_service cycle
    """One transcription cycle: download audio → Groq → re-enqueue."""
    started = time.monotonic()

    # Status reporting: чтобы Telegram-сообщение жило, обновим его на "скачиваю аудио".
    await _set_service_status(
        services, job.message, t("status.audio_download", job.lang), job=job
    )

    download_started = time.monotonic()
    try:
        audio_path = await asyncio.to_thread(services.youtube.download_audio, job.url)
    except Exception as exc:
        # Самые частые кейсы: members-only, Private, geo-block, age-gate,
        # ролик удалён, идущая прямая трансляция. В лог пишем как WARNING
        # (это не баг бота — мы физически не имеем доступа к ролику),
        # пользователю шлём человеческую причину.
        reason = _classify_youtube_download_error(exc, job.lang)
        download_duration = time.monotonic() - download_started
        logger.warning(
            "transcription_queue.audio_download.failed sequence=%s url=%s duration_sec=%.1f reason=%r error=%s",
            job.sequence, job.url, download_duration, reason, exc,
        )
        await _send_transcription_failure(services, job, reason)
        return
    download_duration = time.monotonic() - download_started
    logger.info(
        "transcription_queue.audio_download.done sequence=%s path=%s duration_sec=%.1f",
        job.sequence, audio_path, download_duration,
    )

    await _set_service_status(
        services, job.message, t("status.transcribing", job.lang), job=job
    )

    try:
        segments = await services.groq_whisper.transcribe(Path(audio_path))
    except GroqWhisperUnavailable as exc:
        logger.warning(
            "transcription_queue.groq_unavailable sequence=%s reason=%s",
            job.sequence, exc,
        )
        await _send_transcription_failure(
            services, job,
            t("error.groq_unavailable", job.lang, error=exc),
        )
        _cleanup_audio_file(audio_path)
        return
    except Exception as exc:
        logger.exception("transcription_queue.groq_failed sequence=%s", job.sequence)
        await _send_transcription_failure(
            services, job,
            t("error.groq_failed", job.lang, error=exc),
        )
        _cleanup_audio_file(audio_path)
        return
    finally:
        # Удаляем исходное аудио — даже если Groq упал, оно нам уже не нужно.
        _cleanup_audio_file(audio_path)

    if not segments:
        logger.warning(
            "transcription_queue.empty_result sequence=%s url=%s",
            job.sequence, job.url,
        )
        await _send_transcription_failure(
            services, job, t("error.groq_empty", job.lang)
        )
        return

    duration = time.monotonic() - started
    logger.info(
        "transcription_queue.job.done sequence=%s segments=%s duration_sec=%.1f "
        "(download=%.1fs)",
        job.sequence, len(segments), duration, download_duration,
    )

    # Стамп: транскрипт получен, отдаём обратно в summary_queue.
    job.pre_fetched_segments = list(segments)
    job.pre_fetched_transcript_source = "groq"
    # Job возвращается на обычный путь main worker'а — следующий проход
    # обязан финализировать статус (done/failed) как любой другой job.
    job.routed_to_transcription = False
    # Сбросим pre_fetched_metadata, если он не пришёл — пусть main worker
    # перетянет actual метаданные ролика заново. (Скорее всего тут он None.)

    await _set_service_status(
        services, job.message,
        t("status.transcribe_done", job.lang), job=job,
    )

    async with services.summary_queue_lock:
        await services.summary_queue.put(job)
        if services.summary_worker_task is None or services.summary_worker_task.done():
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))


def _build_context_hint(job: SummaryJob) -> str | None:
    """Build a summarizer context hint for segment-mode (scheduled) jobs."""
    if not job.segment_spans:
        return None
    spans_text = format_spans_for_humans(job.segment_spans)
    experts = ", ".join(job.expert_matches) if job.expert_matches else ""
    if experts:
        return (
            f"Это фрагмент длинного шоу с участием: {experts}. "
            f"Таймкоды фрагмента: {spans_text}. "
            "Саммаризируй только этот фрагмент: весь transcript, который ты получаешь, — "
            "это уже вырезанный кусок. Не упоминай остальную часть ролика."
        )
    return (
        f"Это фрагмент длинного ролика (таймкоды: {spans_text}). "
        "Саммаризируй только этот фрагмент, не упоминай остальную часть ролика."
    )


def _cleanup_audio_file(audio_path) -> None:
    try:
        Path(audio_path).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("transcription_queue.audio_cleanup_failed path=%s error=%s", audio_path, exc)
async def _send_transcription_failure(services: Services, job: SummaryJob, reason: str) -> None:
    """Tell the user the job died in transcription; don't re-enqueue.

    После доставки финального сообщения чистим зависший status («Скачиваю
    аудио…» / «Распознаю…») — иначе у пользователя в чате висят сразу два
    сообщения: бесполезный статус и наш текст с причиной.

    {link} — как в error.generation_failed — собирается ДО подстановки в
    шаблон; здесь это plain-URL (title у транскрипционного отказа не всегда
    известен, сообщение уходит без parse_mode).
    """
    text = t("error.transcript_failed", job.lang, reason=reason, link=job.url)
    try:
        if job.message is not None and not job.scheduled:
            await job.message.answer(text)
        elif services.bot is not None and job.chat_id:
            await services.bot.send_message(
                chat_id=job.chat_id,
                text=text,
                disable_notification=job.disable_notification,
            )
    except Exception:
        logger.exception(
            "transcription_queue.failure_delivery_failed sequence=%s", job.sequence
        )
    # Чистим service-status: пользователь только что увидел финальный текст
    # с причиной, status-сообщение «Скачиваю аудио…» больше не нужно.
    if job.chat_id:
        await _delete_service_status(services, job.chat_id)
    # Транскрипция — тупиковая ветка (job не возвращается в summary_queue),
    # так что это и есть финализация статуса в БД.
    if services.job_store and job.db_id:
        services.job_store.set_status(job.db_id, "failed")
# Подстроки → ключ локали (ytdlp.*) с человеческой причиной. Сравнение
# case-insensitive, на сообщении исключения. Если ничего не подошло — отдадим
# первые 200 символов исходной ошибки, чтобы не молчать (ytdlp.download_failed).
_YT_DLP_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("members-only", "ytdlp.members_only"),
    ("join this channel to get access", "ytdlp.members_only"),
    ("private video", "ytdlp.private"),
    ("video unavailable", "ytdlp.unavailable"),
    ("removed by the uploader", "ytdlp.removed_by_uploader"),
    ("this video has been removed", "ytdlp.removed"),
    ("sign in to confirm your age", "ytdlp.age_restricted"),
    ("sign in to confirm you", "ytdlp.sign_in_required"),
    ("geo restricted", "ytdlp.geo_restricted"),
    ("blocked it in your country", "ytdlp.geo_blocked"),
    ("live event", "ytdlp.live_event"),
    ("premiere", "ytdlp.premiere_not_started"),
    ("this live stream recording is not available", "ytdlp.stream_recording_unavailable"),
)
def _classify_youtube_download_error(exc: Exception, lang: str = "ru") -> str:
    """Map a yt-dlp exception to a friendly localized reason for the user.

    yt-dlp валит сразу в две слоя: ``ExtractorError`` (отказ на этапе
    разбора метаданных, например ``raise_no_formats``) и ``DownloadError``
    (обёртка над ним и над сетевыми сбоями). Сообщения у них в основном
    одинаковые, поэтому сравниваем именно текст по подстрокам.
    """
    raw = str(exc) or exc.__class__.__name__
    lowered = raw.lower()
    for needle, key in _YT_DLP_ERROR_HINTS:
        if needle in lowered:
            return t(key, lang)
    # Никакой known-pattern не подошёл — отдадим обрезанный raw, всё-таки
    # это сообщение от yt-dlp, обычно осмысленное.
    snippet = raw.strip().replace("\n", " ")
    if len(snippet) > 200:
        snippet = snippet[:197].rstrip() + "..."
    return t("ytdlp.download_failed", lang, snippet=snippet)
async def _download_audio_to_chat(
    callback: CallbackQuery,
    video_id: str,
    services: Services,
) -> None:
    """Скачивает аудио YouTube-ролика и шлёт файл в личку как reply к саммари.

    Алгоритм:
    1. Резолвим URL по video_id (через cached summary; если кэша нет — пытаемся
       восстановить «https://www.youtube.com/watch?v=<id>»).
    2. Качаем mp3 через yt-dlp (64 kbps mono — наш дефолт).
    3. Если файл > 49 МБ, ужимаем дальше через ``_ensure_audio_fits_telegram``.
    4. ``send_audio`` в тот же чат, где висит кнопка, с reply на саммари.
    5. На ошибках — отдельным сообщением сообщаем причину.
    """
    if callback.message is None or services.bot is None:
        return
    chat_id = callback.message.chat.id
    summary_message_id = callback.message.message_id

    cache = services.summary_cache
    cached = cache.get(video_id) if cache is not None else None
    if cached is not None and cached.url:
        url = cached.url
        title = cached.title[:64] if cached.title else "YouTube"
        performer = cached.channel_name[:64] if cached.channel_name else "YouTube"
    else:
        # Fallback: соберём URL из video_id. Качества метаданных не будет, но
        # аудио качается.
        url = f"https://www.youtube.com/watch?v={video_id}"
        title = "YouTube"
        performer = "YouTube"

    progress_msg = await services.bot.send_message(
        chat_id=chat_id,
        text="Скачиваю аудио ролика...",
        reply_to_message_id=summary_message_id,
    )

    audio_path: Path | None = None
    try:
        audio_path = await asyncio.to_thread(services.youtube.download_audio, url)
        audio_path = await _ensure_audio_fits_telegram(audio_path)
    except Exception as exc:
        logger.exception("download_audio.failed video_id=%s", video_id)
        try:
            await progress_msg.edit_text(f"Не удалось скачать аудио: {exc}")
        except Exception:
            pass
        return

    try:
        await services.bot.send_audio(
            chat_id=chat_id,
            audio=FSInputFile(str(audio_path)),
            title=title,
            performer=performer,
            reply_to_message_id=summary_message_id,
            disable_notification=True,
        )
        # Промежуточное «скачиваю...» больше не нужно — удалим, чтобы чат
        # не засорять.
        try:
            await progress_msg.delete()
        except Exception:
            pass
    except Exception as exc:
        logger.exception("download_audio.send_failed video_id=%s", video_id)
        try:
            await progress_msg.edit_text(f"Не удалось отправить аудио: {exc}")
        except Exception:
            pass


"""DISABLED: Channel publishing pipeline. Сохранено как docstring чтобы Python
не исполнял эту ветку, но при необходимости легко вернуть.

async def _publish_to_channel(callback, video_id, services):
    chat_id = callback.message.chat.id if callback.message else None
    if chat_id is None:
        return
    cache = services.summary_cache
    cached = cache.get(video_id) if cache is not None else None
    if cached is None:
        await services.bot.send_message(chat_id=chat_id, text="Не нашёл саммари в кэше.")
        return
    posts = services.channel_posts
    target_channel_id = services.settings.telegram_publish_channel_id
    if posts is None or target_channel_id is None or services.bot is None:
        await services.bot.send_message(chat_id=chat_id, text="Канал не настроен.")
        return
    fresh_comments = await _refresh_cached_comments(cached, services, source_label="channel-publish")
    existing = posts.get(video_id)
    if existing is not None and existing.chat_id == target_channel_id:
        # ... editMessageCaption flow с refresh комментариев
        ...
    # ... новая публикация: download_audio → _ensure_audio_fits_telegram → send_audio
    # ... сохранение ChannelPost в store

def _chat_id_to_link(chat_id):
    s = str(chat_id)
    if s.startswith("-100"):
        return s[4:]"""


# Скрытая копия helper'а, оставлена для смежного использования вне канала.
def _chat_id_to_link(chat_id: int) -> str:  # noqa: F811
    """Convert ``-1001234567890`` channel id to the ``1234567890`` form used in
    ``t.me/c/<id>/<message_id>`` deep links."""
    s = str(chat_id)
    if s.startswith("-100"):
        return s[4:]
    if s.startswith("-"):
        return s[1:]
    return s
async def _ensure_audio_fits_telegram(audio_path: Path) -> Path:
    """If audio file > Telegram limit, recompress aggressively.

    Defaulту download_audio уже даёт 32kbps mono mp3 — это покрывает ролики
    до ~3.5 часов в 50 МБ. Fallback нужен только для марафонов или если
    YouTube/yt-dlp выдал большой исходник по какой-то причине. Дожимаем
    до Whisper-grade 24kbps mono 16kHz (~11 МБ/час, лезет ~4.5 часа).
    Если и это не помогло — поднимаем исключение, caller отправит без аудио.
    """
    try:
        size = audio_path.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"Не удалось проверить размер аудио: {exc}") from exc
    if size <= TELEGRAM_AUDIO_MAX_BYTES:
        logger.info(
            "channel.audio.size_ok path=%s size_bytes=%s", audio_path, size,
        )
        return audio_path

    output_path = audio_path.with_suffix(audio_path.suffix + ".tg.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-vn",
        "-ac", "1",                  # mono
        "-ar", "16000",              # 16kHz — для речи достаточно
        "-c:a", "libmp3lame",
        "-b:a", "24k",               # ~11 МБ/час
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace")[:500] if stderr else ""
        raise RuntimeError(f"ffmpeg.compress failed (rc={proc.returncode}): {err}")
    new_size = output_path.stat().st_size
    logger.info(
        "channel.audio.compressed input=%s input_size=%s output=%s output_size=%s",
        audio_path, size, output_path, new_size,
    )
    if new_size > TELEGRAM_AUDIO_MAX_BYTES:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Аудио после компрессии всё равно {new_size / 1024 / 1024:.1f} МБ, "
            f"превышает Telegram-лимит {TELEGRAM_AUDIO_MAX_BYTES / 1024 / 1024:.0f} МБ. "
            "Видео слишком длинное для отправки одним файлом."
        )
    return output_path


"""DISABLED helpers для канал-публикации. Сохранены как docstring чтобы Python
не объявлял функции в глобальном scope, но текст легко вернуть.

def _format_channel_post_caption(cached, fresh_comments):
    # builds <=1024 char HTML caption: channel/title/overview/telegraph/tags/top-comment
    ...

def _channel_top_comment_line(c):
    # one-line: 💬 «text»... — N лайков
    ...

def _truncate_plain(text, max_chars):
    # plain-text трим с многоточием
    ...
"""


# Лимиты Telegram + размеры аудио, при которых имеет смысл что-то делать.
TELEGRAM_AUDIO_MAX_BYTES = 49 * 1024 * 1024   # 50 МБ - 1 МБ запас
