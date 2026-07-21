from __future__ import annotations

import asyncio
import logging
import time

from aiogram.types import Message

from app.i18n import t
from app.monitoring_service import ScheduledCandidate
from app.morning_digest import maybe_send_morning_digest

from app.services_container import Services, SummaryJob
from app.status_messages import (
    _bump_service_status,
    _format_elapsed,
    _prefetch_job_title,
    _set_service_status,
)
from app.utils import extract_video_id
from app.delivery import (
    _deliver_cached_summary_for_job,
    _lookup_cached_summary,
    _message_user_id,
    _send_cached_summary_to_chat,
    _send_quota_denied,
)
from app.pipeline import _process_transcription_job, _process_youtube_job


logger = logging.getLogger(__name__)


# Ранги приоритета summary_queue (см. SummaryJob.priority в services_container.py).
PRIORITY_PRIVILEGED = 0  # owner, allowlist, платные подписчики
PRIORITY_FREE = 1        # внешние free-пользователи
PRIORITY_SCHEDULED = 2   # scheduled-мониторинг каналов


def _job_priority(user_id: int | None, services: Services) -> int:
    """Ранг приоритета живого (не scheduled) запроса по user_id.

    owner/allowlist и активные подписчики — PRIORITY_PRIVILEGED (0), все
    остальные внешние пользователи — PRIORITY_FREE (1). Scheduled-задачи
    ранг не запрашивают тут — им всегда PRIORITY_SCHEDULED (2), назначается
    прямо в месте постановки в очередь.
    """
    if services.users.is_allowed(user_id):
        return PRIORITY_PRIVILEGED
    if services.billing is not None and user_id is not None and services.billing.is_subscriber(user_id):
        return PRIORITY_PRIVILEGED
    return PRIORITY_FREE


async def _find_duplicate_job(
    video_id: str, chat_id: int, services: Services
) -> SummaryJob | None:
    """Ищем уже стоящий в очереди/обрабатываемый job на тот же (video_id, chat_id).

    Дедупликация повторных кликов по одному ролику: без неё каждый клик
    ставит новый job (и для внешних пользователей списывает квоту за каждый
    клик). Проверяем summary_queue (активный + ожидающие) и, если он
    инициализирован, transcription_queue (активный + ожидающие) — ролик может
    застрять там в ожидании Groq. Каждая очередь читается под своим локом по
    отдельности (как в _format_queue_status) — без вложенных локов, риска
    дедлока нет.
    """
    candidates: list[SummaryJob] = []
    async with services.summary_queue_lock:
        if services.summary_active_job is not None:
            candidates.append(services.summary_active_job)
        candidates.extend(services.summary_queue._queue)

    # getattr with default: some lightweight test fakes for Services don't
    # define transcription_* attributes at all (they predate the
    # transcription queue) — treat that the same as "not initialized".
    transcription_queue = getattr(services, "transcription_queue", None)
    transcription_queue_lock = getattr(services, "transcription_queue_lock", None)
    if transcription_queue is not None and transcription_queue_lock is not None:
        async with transcription_queue_lock:
            transcription_active_job = getattr(services, "transcription_active_job", None)
            if transcription_active_job is not None:
                candidates.append(transcription_active_job)
            candidates.extend(transcription_queue._queue)

    for candidate in candidates:
        if candidate.chat_id != chat_id:
            continue
        try:
            candidate_video_id = extract_video_id(candidate.url)
        except Exception:  # noqa: BLE001 — перестраховка, в очереди только видео-URL
            continue
        if candidate_video_id == video_id:
            return candidate
    return None


async def _enqueue_summary_job(message: Message, url: str, services: Services) -> None:
    # Внешний пользователь (не allowlist) в PUBLIC_MODE проходит через квоты.
    from app.bot_handlers import _is_allowed, _msg_lang  # local: избегаем цикла
    lang = _msg_lang(message, services)
    quota_user_id: int | None = None
    if not _is_allowed(message, services) and message.from_user is not None:
        quota_user_id = message.from_user.id

    enqueued = False
    try:
        # Cache hit fast-path: если по этому ролику уже было саммари, отдаём его
        # сразу, не занимая очередь и не дёргая LLM/Whisper.
        cached = _lookup_cached_summary(url, services, lang=lang)
        if cached is not None:
            logger.info(
                "queue.cache.hit chat_id=%s video_id=%s telegraph_url=%s",
                message.chat.id, cached.video_id, cached.telegraph_url,
            )
            await _send_cached_summary_to_chat(message, cached, services)
            enqueued = True
            return

        # Дубль-проверка ДО квота-гейта: повторный клик по тому же ролику в
        # том же чате не должен жечь квоту внешнего пользователя, даже если
        # исходный job ещё не обработан.
        try:
            incoming_video_id: str | None = extract_video_id(url)
        except Exception:  # noqa: BLE001 — на этом пути всегда видео-URL, перестраховка
            incoming_video_id = None
        if incoming_video_id is not None:
            duplicate = await _find_duplicate_job(incoming_video_id, message.chat.id, services)
            if duplicate is not None:
                logger.info(
                    "queue.job.duplicate_skipped chat_id=%s video_id=%s",
                    message.chat.id, incoming_video_id,
                )
                await message.answer(t("status.already_queued", lang))
                # Чат чистый: исходную ссылку удаляем как при обычной
                # постановке в очередь — статус-сообщение существующего job'а
                # и так висит и покажет прогресс.
                enqueued = True
                return

        if quota_user_id is not None and services.quota is not None:
            verdict = services.quota.check(quota_user_id)
            if not verdict.allowed:
                await _send_quota_denied(message, services, verdict, lang)
                return  # сообщение пользователя НЕ удаляем — finally ниже пропустит

        active_job: SummaryJob | None
        async with services.summary_queue_lock:
            services.summary_next_sequence += 1
            sequence = services.summary_next_sequence
            active_job = services.summary_active_job
            # Snapshot до put(): считаем, сколько ожидающих job'ов встанут
            # перед новым по (priority, sequence) — вклинивание приоритета
            # меняет "позицию в очереди", которую видит пользователь.
            pending_snapshot = list(services.summary_queue._queue)
            priority = _job_priority(_message_user_id(message), services)
            db_id = (
                services.job_store.add(
                    url, message.chat.id, scheduled=False, disable_notification=False,
                    title_hint=None, lang=lang,
                )
                if services.job_store
                else None
            )
            job = SummaryJob(
                sequence=sequence,
                message=message,
                url=url,
                enqueued_at=time.monotonic(),
                chat_id=message.chat.id,
                db_id=db_id,
                quota_user_id=quota_user_id,
                lang=lang,
                priority=priority,
            )
            ahead = sum(
                1
                for pending in pending_snapshot
                if (pending.priority, pending.sequence) < (job.priority, job.sequence)
            )
            position = (1 if active_job is not None else 0) + ahead + 1
            await services.summary_queue.put(job)
            enqueued = True
            if services.summary_worker_task is None or services.summary_worker_task.done():
                services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))

            logger.info(
                "queue.job.enqueued sequence=%s chat_id=%s position=%s pending=%s url=%s",
                sequence,
                message.chat.id,
                position,
                services.summary_queue.qsize(),
                url,
            )

        asyncio.create_task(_prefetch_job_title(job, services))

        if position == 1:
            await _set_service_status(
                services=services,
                source_message=message,
                text=t("status.queued_first", lang),
                job=job,
                bump=True,
            )
        elif active_job is not None and active_job.chat_id == message.chat.id:
            await _bump_service_status(services, message, active_job)
        else:
            await _set_service_status(
                services=services,
                source_message=message,
                text=t("status.queued_position", lang, position=position),
                job=job,
                bump=True,
            )
    finally:
        # Удаляем исходное сообщение пользователя с YouTube-ссылкой сразу же,
        # как только ссылка попала в очередь (или была обслужена из кэша).
        # Так превью ссылки в Telegram не дублирует шапку нашей доставки/статуса
        # и чат остаётся чистым на всё время обработки. Бот может удалить
        # user-message в private chat в течение 48 часов; ошибки swallow'им,
        # потому что удаление best-effort и не влияет на саму доставку саммари.
        # При отказе по квоте (enqueued=False) сообщение с ссылкой оставляем —
        # пользователь должен видеть, что именно он присылал, когда получил отказ.
        if enqueued:
            try:
                await message.delete()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "source_message.delete_failed chat_id=%s error=%s",
                    message.chat.id, exc,
                )
async def enqueue_scheduled_candidate(
    candidate: ScheduledCandidate, channel, services: Services
) -> None:
    """Enqueue a summary job for a scheduled monitoring hit.

    Called by MonitoringService after the filter pipeline has accepted a video.
    """
    target_chat_id = services.settings.monitoring_target_chat_id
    if target_chat_id is None:
        logger.warning(
            "monitoring.enqueue.no_target_chat video_id=%s channel_id=%s",
            candidate.metadata.video_id,
            channel.channel_id,
        )
        return

    async with services.summary_queue_lock:
        services.summary_next_sequence += 1
        sequence = services.summary_next_sequence
        title_hint = candidate.metadata.title or candidate.feed_entry.title
        db_id = (
            services.job_store.add(
                candidate.feed_entry.url,
                target_chat_id,
                scheduled=True,
                disable_notification=True,
                title_hint=title_hint,
                lang="ru",
            )
            if services.job_store
            else None
        )
        job = SummaryJob(
            sequence=sequence,
            message=None,
            url=candidate.feed_entry.url,
            enqueued_at=time.monotonic(),
            chat_id=target_chat_id,
            title_hint=title_hint,
            scheduled=True,
            disable_notification=True,
            pre_fetched_metadata=candidate.metadata,
            pre_fetched_segments=list(candidate.transcript_segments) or None,
            pre_fetched_transcript_source=candidate.transcript_source,
            segment_spans=list(candidate.segment_spans) or None,
            expert_matches=list(candidate.expert_matches) or None,
            show_matches=list(candidate.show_matches) or None,
            db_id=db_id,
            lang="ru",
            priority=PRIORITY_SCHEDULED,
        )
        await services.summary_queue.put(job)
        if services.summary_worker_task is None or services.summary_worker_task.done():
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))

    logger.info(
        "monitoring.enqueue.scheduled sequence=%s video_id=%s channel_id=%s experts=%s spans=%s",
        sequence,
        candidate.metadata.video_id,
        channel.channel_id,
        candidate.expert_matches,
        candidate.segment_spans,
    )
async def restore_pending_jobs(services: Services) -> int:
    """Восстановить незавершённые job'ы из БД после рестарта контейнера.

    message=None: доставка результата пойдёт через bot.send_message(chat_id) —
    тот же путь, что у scheduled-задач. pre_fetched-данные не персистились,
    metadata/субтитры будут получены заново (кэш саммари при этом продолжает
    отсекать полные повторы).
    """
    if services.job_store is None:
        return 0
    rows = services.job_store.pending()
    restored = 0
    async with services.summary_queue_lock:
        for row in rows:
            services.summary_next_sequence += 1
            scheduled = bool(row["scheduled"])
            # chat_id восстановленной задачи — private chat, где chat_id ==
            # user_id (owner/allowlist/подписчик проверяются как обычно).
            # Scheduled-строки ранг по chat_id не проверяют — им всегда
            # PRIORITY_SCHEDULED, приоритет не персистится в БД.
            priority = (
                PRIORITY_SCHEDULED if scheduled else _job_priority(row["chat_id"], services)
            )
            job = SummaryJob(
                sequence=services.summary_next_sequence,
                message=None,
                url=row["url"],
                enqueued_at=time.monotonic(),
                chat_id=row["chat_id"],
                title_hint=row["title_hint"],
                scheduled=scheduled,
                disable_notification=bool(row["disable_notification"]),
                db_id=row["id"],
                lang=row["lang"],
                priority=priority,
                # Q4: перенос счётчика транзиентных ретраев — если контейнер
                # рестартовал во время "active" job'а, который уже пережил
                # 1-2 сетевых сбоя, восстановленный job не должен начинать
                # отсчёт заново (иначе лимит в 3 попытки эффективно не работает
                # через рестарты).
                retry_count=row["attempts"] if "attempts" in row.keys() else 0,
            )
            services.job_store.set_status(row["id"], "queued")
            await services.summary_queue.put(job)
            restored += 1
        if restored and (services.summary_worker_task is None or services.summary_worker_task.done()):
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))
    if restored:
        logger.info("queue.restored jobs=%s", restored)
    return restored


async def enqueue_local_api_job(video_id: str, services: Services) -> str:
    """Постановка от локального API (кнопка расширения, HTTP без Message).

    Аналог восстановленных задач: message=None, доставка и статусы идут через
    bot.send_message(chat_id владельца). Квоты не применяются (owner).
    Возвращает "cached" (отдано из кэша) или "queued".
    """
    owner_id = services.settings.owner_user_id
    if owner_id is None:
        raise RuntimeError("owner_not_configured")
    lang = "ru"
    if services.user_langs is not None:
        stored = services.user_langs.get(owner_id)
        if stored is not None:
            lang = stored[0]
    url = f"https://www.youtube.com/watch?v={video_id}"

    cached = _lookup_cached_summary(url, services, lang=lang)
    if cached is not None:
        probe = SummaryJob(
            sequence=0, message=None, url=url,
            enqueued_at=time.monotonic(), chat_id=owner_id, lang=lang,
        )
        # Доставка — несколько Telegram-вызовов (секунды), а расширение ждёт
        # HTTP-ответ всего 1.5 сек: превысим — кнопка откатится на deep-link
        # и Telegram откроется зря. Поэтому доставляем в фоновой задаче,
        # ответ отдаём сразу.
        task = asyncio.create_task(
            _deliver_cached_for_local_api(probe, services, cached, video_id)
        )
        _local_api_delivery_tasks.add(task)
        task.add_done_callback(_local_api_delivery_tasks.discard)
        logger.info("local_api.cache.hit video_id=%s", video_id)
        return "cached"

    # Дубль-проверка: тот же ролик уже стоит/обрабатывается для владельца —
    # новый job не создаём (иначе кнопка расширения плодила бы по job'у на
    # каждый клик). Кнопка всё равно мигнёт ✅ — ролик и так в очереди.
    duplicate = await _find_duplicate_job(video_id, owner_id, services)
    if duplicate is not None:
        logger.info("local_api.job.duplicate_skipped video_id=%s", video_id)
        return "queued"

    active_job: SummaryJob | None
    async with services.summary_queue_lock:
        services.summary_next_sequence += 1
        db_id = (
            services.job_store.add(
                url, owner_id, scheduled=False, disable_notification=False,
                title_hint=None, lang=lang,
            )
            if services.job_store
            else None
        )
        active_job = services.summary_active_job
        # Snapshot до put() — см. тот же паттерн в _enqueue_summary_job:
        # считаем, сколько ожидающих job'ов встанут перед новым по
        # (priority, sequence), чтобы статус показал верную позицию.
        pending_snapshot = list(services.summary_queue._queue)
        job = SummaryJob(
            sequence=services.summary_next_sequence,
            message=None,
            url=url,
            enqueued_at=time.monotonic(),
            chat_id=owner_id,
            db_id=db_id,
            lang=lang,
            priority=PRIORITY_PRIVILEGED,
        )
        ahead = sum(
            1
            for pending in pending_snapshot
            if (pending.priority, pending.sequence) < (job.priority, job.sequence)
        )
        position = (1 if active_job is not None else 0) + ahead + 1
        await services.summary_queue.put(job)
        if services.summary_worker_task is None or services.summary_worker_task.done():
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))
    logger.info(
        "local_api.job.enqueued sequence=%s video_id=%s position=%s", job.sequence, video_id, position
    )

    # Тот же паттерн начального статуса, что и в _enqueue_summary_job, но
    # через bot.send_message (message=None) — до фикса Q2 расширение ставило
    # job молча, без единого сообщения о процессе/очереди в чате.
    if position == 1:
        await _set_service_status(
            services=services,
            source_message=None,
            text=t("status.queued_first", lang),
            job=job,
            bump=True,
        )
    elif active_job is not None and active_job.chat_id == owner_id:
        # В чате owner'а уже идёт генерация: не затираем её живой прогресс
        # текстом «Позиция: N» — bump'аем статус АКТИВНОГО job'а (новый ролик
        # виден в queue-блоке под ним), как в _enqueue_summary_job.
        await _bump_service_status(services, None, active_job)
    else:
        await _set_service_status(
            services=services,
            source_message=None,
            text=t("status.queued_position", lang, position=position),
            job=job,
            bump=True,
        )
    return "queued"


# Сильные ссылки на фоновые задачи доставки кэша: голый create_task без
# ссылки может быть собран GC до завершения.
_local_api_delivery_tasks: set[asyncio.Task] = set()


async def _deliver_cached_for_local_api(job, services, cached, video_id: str) -> None:
    try:
        await _deliver_cached_summary_for_job(job, services, cached)
    except Exception:  # noqa: BLE001
        logger.exception("local_api.cached_delivery_failed video_id=%s", video_id)


# Как часто deferred-scheduler проверяет, не пришло ли время отложенных
# премьер. 5 минут: точность «через 4 часа после выхода» ±5 мин достаточна.
DEFERRED_POLL_INTERVAL_SEC = 300


async def run_deferred_jobs_scheduler(services: Services) -> None:
    """Фоновый цикл: поднимает отложенные премьеры (status='deferred'),
    у которых наступил run_after, обратно в summary_queue."""
    logger.info("deferred.scheduler.start")
    try:
        while True:
            try:
                await _requeue_due_deferred(services)
            except Exception:
                logger.exception("deferred.scheduler.tick_failed")
            await asyncio.sleep(DEFERRED_POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        logger.info("deferred.scheduler.cancelled")
        raise


async def _requeue_due_deferred(services: Services) -> None:
    if services.job_store is None:
        return
    rows = services.job_store.due_deferred(time.time())
    if not rows:
        return
    async with services.summary_queue_lock:
        for row in rows:
            services.summary_next_sequence += 1
            scheduled = bool(row["scheduled"])
            priority = (
                PRIORITY_SCHEDULED if scheduled else _job_priority(row["chat_id"], services)
            )
            job = SummaryJob(
                sequence=services.summary_next_sequence,
                message=None,
                url=row["url"],
                enqueued_at=time.monotonic(),
                chat_id=row["chat_id"],
                title_hint=row["title_hint"],
                scheduled=scheduled,
                disable_notification=bool(row["disable_notification"]),
                db_id=row["id"],
                lang=row["lang"],
                priority=priority,
                # Q4: перенос attempts — премьеры проходят тут с attempts=0
                # (не тронуты транзиентным ретраем) и остаются на 0; job'ы,
                # отложенные из-за сетевого сбоя, продолжают отсчёт лимита в
                # 3 попытки через requeue, а не сбрасывают его.
                retry_count=row["attempts"] if "attempts" in row.keys() else 0,
            )
            services.job_store.set_status(row["id"], "queued")
            await services.summary_queue.put(job)
            logger.info(
                "deferred.requeued db_id=%s chat_id=%s url=%s",
                row["id"], row["chat_id"], row["url"],
            )
        if services.summary_worker_task is None or services.summary_worker_task.done():
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))
async def _is_llm_available(services: Services) -> bool:
    try:
        await asyncio.wait_for(services.llm.list_models(), timeout=10)
        return True
    except Exception as exc:
        logger.info("llm.health.unavailable provider=%s error=%s", services.llm.provider_name, exc)
        return False
async def _summary_queue_worker(services: Services) -> None:
    logger.info("queue.worker.start")
    try:
        while True:
            job: SummaryJob | None = None
            try:
                job = await asyncio.wait_for(services.summary_queue.get(), timeout=1)
            except TimeoutError:
                async with services.summary_queue_lock:
                    if services.summary_queue.empty():
                        services.summary_worker_task = None
                        services.summary_next_sequence = 0
                        logger.info("queue.worker.stop")
                        return
                    continue

            if job.scheduled and not await _is_llm_available(services):
                # DB-статус job_store здесь сознательно не трогаем: обработка
                # ещё не начиналась, так что "queued" — точное состояние job'а
                # на всё время деферрала. Если контейнер перезапустится во время
                # долгого даунтайма LLM, restore_pending_jobs корректно поднимет
                # его из БД именно как queued-работу.
                retry_interval = services.settings.monitoring_llm_retry_interval_sec
                job.retry_count += 1
                logger.info(
                    "queue.job.defer_scheduled sequence=%s chat_id=%s retry=%s sleep_sec=%s",
                    job.sequence,
                    job.chat_id,
                    job.retry_count,
                    retry_interval,
                )
                async with services.summary_queue_lock:
                    services.summary_active_job = None
                await services.summary_queue.put(job)
                services.summary_queue.task_done()
                await asyncio.sleep(max(30, retry_interval))
                continue

            async with services.summary_queue_lock:
                services.summary_active_job = job
            if services.job_store and job.db_id:
                services.job_store.set_status(job.db_id, "active")

            wait_sec = time.monotonic() - job.enqueued_at
            logger.info(
                "queue.job.start sequence=%s chat_id=%s wait_sec=%.1f pending=%s url=%s scheduled=%s",
                job.sequence,
                job.chat_id,
                wait_sec,
                services.summary_queue.qsize(),
                job.url,
                job.scheduled,
            )
            try:
                await _process_youtube_job(job, services)
                logger.info(
                    "queue.job.done sequence=%s chat_id=%s pending=%s",
                    job.sequence,
                    job.chat_id,
                    services.summary_queue.qsize(),
                )
                # Маршрут «нет субтитров → transcription_queue» не финализирует
                # статус: job вернётся сюда после Groq, остаётся "active".
                # Отложенные премьеры (deferred_until) финализировать тоже
                # нельзя — pipeline уже проставил "deferred" + run_after.
                if (
                    services.job_store
                    and job.db_id
                    and not job.routed_to_transcription
                    and job.deferred_until is None
                ):
                    services.job_store.set_status(job.db_id, "done")
            except asyncio.CancelledError:
                logger.info("queue.job.cancelled sequence=%s chat_id=%s", job.sequence, job.chat_id)
                if services.job_store and job.db_id:
                    services.job_store.set_status(job.db_id, "cancelled")
                raise
            except Exception:
                logger.exception("queue.job.failed sequence=%s url=%s", job.sequence, job.url)
                if services.job_store and job.db_id:
                    services.job_store.set_status(job.db_id, "failed")
            finally:
                async with services.summary_queue_lock:
                    if services.summary_active_job == job:
                        services.summary_active_job = None
                    if services.summary_queue.empty():
                        services.summary_next_sequence = 0
                services.summary_queue.task_done()

            if job.scheduled:
                try:
                    await maybe_send_morning_digest(services)
                except Exception:
                    logger.exception("morning_digest.trigger_failed")
    except asyncio.CancelledError:
        logger.info("queue.worker.cancelled")
        async with services.summary_queue_lock:
            if services.summary_worker_task is asyncio.current_task():
                services.summary_worker_task = None
            services.summary_active_job = None
            if services.summary_queue.empty():
                services.summary_next_sequence = 0
    except Exception:
        logger.exception("queue.worker.failed")
        async with services.summary_queue_lock:
            services.summary_worker_task = None
            services.summary_active_job = None
            if services.summary_queue.empty():
                services.summary_next_sequence = 0
async def _enqueue_transcription_job(job: SummaryJob, services: Services) -> None:
    """Push a job into the transcription queue and ensure a worker is running.

    Called from the main worker when it learns YouTube has no captions for
    the video. The transcription worker downloads audio, calls Groq Whisper,
    populates ``job.pre_fetched_segments`` and re-enqueues to summary_queue.
    """
    if services.transcription_queue is None or services.transcription_queue_lock is None:
        raise RuntimeError(
            "transcription_queue не инициализирована (см. main.py при старте бота)."
        )
    async with services.transcription_queue_lock:
        await services.transcription_queue.put(job)
        if (
            services.transcription_worker_task is None
            or services.transcription_worker_task.done()
        ):
            services.transcription_worker_task = asyncio.create_task(
                _transcription_queue_worker(services)
            )
        logger.info(
            "transcription_queue.enqueued sequence=%s pending=%s url=%s",
            job.sequence,
            services.transcription_queue.qsize(),
            job.url,
        )
async def _transcription_queue_worker(services: Services) -> None:
    """Background worker that processes the transcription queue.

    Pulls one job at a time:
    1. Downloads audio via yt-dlp.
    2. Calls Groq Whisper (free tier, multipart upload, returns segments).
    3. Stamps the job with ``pre_fetched_segments`` + ``pre_fetched_transcript_source="groq"``.
    4. Pushes the job back to ``summary_queue`` so the main worker continues
       the regular path (no second transcript-fetch attempt — pre_fetched is
       respected).

    Failures are logged + reported to the user; job is dropped (not retried).
    """
    logger.info("transcription_queue.worker.start")
    queue = services.transcription_queue
    if queue is None or services.transcription_queue_lock is None:
        return
    try:
        while True:
            job: SummaryJob | None = None
            try:
                job = await asyncio.wait_for(queue.get(), timeout=1)
            except TimeoutError:
                async with services.transcription_queue_lock:
                    if queue.empty():
                        services.transcription_worker_task = None
                        logger.info("transcription_queue.worker.stop")
                        return
                    continue

            async with services.transcription_queue_lock:
                services.transcription_active_job = job

            logger.info(
                "transcription_queue.job.start sequence=%s url=%s pending=%s",
                job.sequence, job.url, queue.qsize(),
            )
            try:
                await _process_transcription_job(job, services)
            except asyncio.CancelledError:
                # Не покрыто брифом напрямую: воркер транскрипции сегодня не
                # отменяется явно ни из /stop, ни где-либо ещё, но может
                # получить CancelledError при остановке процесса (shutdown).
                # По смыслу это "cancelled", а не "failed".
                if services.job_store and job.db_id:
                    services.job_store.set_status(job.db_id, "cancelled")
                raise
            except Exception:
                logger.exception(
                    "transcription_queue.job.failed sequence=%s url=%s",
                    job.sequence, job.url,
                )
            finally:
                async with services.transcription_queue_lock:
                    if services.transcription_active_job == job:
                        services.transcription_active_job = None
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("transcription_queue.worker.cancelled")
        async with services.transcription_queue_lock:
            if services.transcription_worker_task is asyncio.current_task():
                services.transcription_worker_task = None
            services.transcription_active_job = None
    except Exception:
        logger.exception("transcription_queue.worker.failed")
        async with services.transcription_queue_lock:
            services.transcription_worker_task = None
            services.transcription_active_job = None
async def _stop_summary_queue(message: Message, services: Services) -> None:
    async with services.summary_queue_lock:
        active = services.summary_active_job
        pending_count = _drain_summary_queue(services.summary_queue, services)
        worker_task = services.summary_worker_task
        services.summary_next_sequence = 0
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()

    if active is None and pending_count == 0:
        await message.answer("Очередь summary уже пуста.")
        return

    if active is not None:
        await message.answer(f"Останавливаю текущую генерацию и очищаю очередь. Удалено из очереди: {pending_count}.")
    else:
        await message.answer(f"Очередь summary очищена. Удалено из очереди: {pending_count}.")
def _drain_summary_queue(queue: asyncio.PriorityQueue[SummaryJob], services: Services) -> int:
    count = 0
    while True:
        try:
            job = queue.get_nowait()
        except asyncio.QueueEmpty:
            return count
        if services.job_store and job.db_id:
            services.job_store.set_status(job.db_id, "cancelled")
        queue.task_done()
        count += 1
async def _format_queue_status(services: Services) -> str:
    async with services.summary_queue_lock:
        summary_active = services.summary_active_job
        summary_pending = services.summary_queue.qsize()

    transcription_active = None
    transcription_pending = 0
    if (
        services.transcription_queue is not None
        and services.transcription_queue_lock is not None
    ):
        async with services.transcription_queue_lock:
            transcription_active = services.transcription_active_job
            transcription_pending = services.transcription_queue.qsize()

    nothing_in_summary = summary_active is None and summary_pending == 0
    nothing_in_transcription = transcription_active is None and transcription_pending == 0
    if nothing_in_summary and nothing_in_transcription:
        return "Все очереди пусты."

    lines: list[str] = []
    lines.append("📝 Очередь summary:")
    if summary_active is not None:
        elapsed = int(time.monotonic() - summary_active.enqueued_at)
        lines.append(
            f"  Сейчас: #{summary_active.sequence} ({_format_elapsed(elapsed)} в очереди)"
        )
    else:
        lines.append("  Сейчас: ничего не обрабатывается")
    lines.append(f"  Ожидают: {summary_pending}")

    lines.append("")
    lines.append("🎙 Очередь распознавания (Groq Whisper):")
    if transcription_active is not None:
        elapsed = int(time.monotonic() - transcription_active.enqueued_at)
        lines.append(
            f"  Сейчас: #{transcription_active.sequence} ({_format_elapsed(elapsed)} в очереди)"
        )
    else:
        lines.append("  Сейчас: ничего не распознаётся")
    lines.append(f"  Ожидают: {transcription_pending}")
    return "\n".join(lines)
