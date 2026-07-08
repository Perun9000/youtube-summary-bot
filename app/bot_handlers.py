from __future__ import annotations

import asyncio
import datetime
import logging
import time
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from app import log_analytics
from app.billing import MONTH_SEC
from app.digest_service import render_digest_html
from app.i18n import LANG_NATIVE_NAMES, SUPPORTED_LANGS, normalize_language_code, t
from app.llm_client import OpenRouterClient, health_check_with_reason
from app.monitoring_service import ScanProgress
from app.utils import (
    classify_youtube_url,
    escape_html,
    extract_first_url,
    extract_video_id,
    extract_youtube_url,
)

# Re-exports: внешние точки входа (main.py) продолжают импортировать эти имена
# из app.bot_handlers.
from app.services_container import (  # noqa: F401
    MAX_TELEGRAM_MESSAGE_CHARS,
    PENDING_ADMIN_TIMEOUT_SEC,
    PendingAdminInput,
    Services,
    SummaryJob,
    YOUTUBE_VIDEO_ID_RE as _YOUTUBE_VIDEO_ID_RE,
)
from app.queue_service import (  # noqa: F401
    enqueue_scheduled_candidate,
    restore_pending_jobs,
    _enqueue_summary_job,
    _format_queue_status,
    _stop_summary_queue,
)
from app.delivery import _message_user_id  # noqa: F401
from app.pipeline import _download_audio_to_chat  # noqa: F401
from app.transcript_export import pretty_transcript_filename, transcript_path


logger = logging.getLogger(__name__)

MAX_SYSTEM_PROMPT_CHARS = 8000

SUBSCRIPTION_PAYLOAD = "monthly_summary_subscription"


def _subscription_until_from_payment(payment, now: float | None = None) -> float:
    """Срок подписки из SuccessfulPayment.subscription_expiration_date.

    По Bot API это Unix-время (int); некоторые версии aiogram отдают его как
    datetime — поддерживаем оба варианта. Если поля нет — 30 дней от now."""
    now = now if now is not None else time.time()
    expiration = getattr(payment, "subscription_expiration_date", None)
    if expiration is not None:
        to_timestamp = getattr(expiration, "timestamp", None)
        return to_timestamp() if callable(to_timestamp) else float(expiration)
    return now + MONTH_SEC


async def _send_subscription_invoice(chat_id: int, services: Services, lang: str) -> None:
    """Инвойс нативной Stars-подписки (валюта XTR, автопродление 30 дней).

    subscription_period поддерживается только в createInvoiceLink, поэтому
    шлём ссылку кнопкой, а не send_invoice.
    """
    s = services.settings
    link = await services.bot.create_invoice_link(
        title=t("subscribe.invoice_title", lang),
        description=t("subscribe.invoice_description", lang, monthly=s.quota_sub_monthly),
        payload=SUBSCRIPTION_PAYLOAD,
        currency="XTR",
        prices=[LabeledPrice(
            label=t("subscribe.invoice_label", lang), amount=s.subscription_price_stars
        )],
        subscription_period=2592000,
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=t("subscribe.pay_button", lang, price=s.subscription_price_stars), url=link
        )
    ]])
    await services.bot.send_message(
        chat_id=chat_id,
        text=t(
            "subscribe.pitch",
            lang,
            monthly=s.quota_sub_monthly,
            price=s.subscription_price_stars,
        ),
        reply_markup=keyboard,
    )


def resolve_user_lang(
    user_id: int | None, language_code: str | None, services: Services
) -> str:
    """Резолв языка пользователя: manual > сохранённый auto > autodetect > en.

    Никогда не кидает — недоступный store или битый user_id просто
    пропускают персист и падают на normalize_language_code.
    """
    store = getattr(services, "user_langs", None)
    if user_id is not None and store is not None:
        try:
            existing = store.get(user_id)
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None:
            return existing[0]
        lang = normalize_language_code(language_code)
        try:
            store.set(user_id, lang, "auto")
        except Exception:  # noqa: BLE001
            logger.exception("lang.auto_persist_failed user_id=%s", user_id)
        return lang
    return normalize_language_code(language_code)


def _msg_lang(message: Message, services: Services) -> str:
    return resolve_user_lang(
        _message_user_id(message),
        message.from_user.language_code if message.from_user else None,
        services,
    )


def _cb_lang(callback: CallbackQuery, services: Services) -> str:
    """Тот же резолв, что и `_msg_lang`, но для callback_query (кнопки)."""
    user_id = callback.from_user.id if callback.from_user else None
    language_code = callback.from_user.language_code if callback.from_user else None
    return resolve_user_lang(user_id, language_code, services)


def _language_keyboard() -> InlineKeyboardMarkup:
    """7 кнопок выбора языка (LANG_NATIVE_NAMES), по 2 в ряд."""
    buttons = [
        InlineKeyboardButton(text=LANG_NATIVE_NAMES[code], callback_data=f"lang:{code}")
        for code in SUPPORTED_LANGS
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_router(services: Services) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        lang = _msg_lang(message, services)
        if not _has_access(message, services):
            await message.answer(t("access.closed", lang))
            return
        # Telegram передаёт payload deep-link'а как второй токен команды:
        # "/start <video_id>". Используется browser-extension'ом — кнопка
        # «Summary» внутри UI YouTube открывает https://t.me/<bot>?start=<id>,
        # Telegram-клиент сам отправляет /start <id>, мы парсим и enqueue'им.
        payload = ""
        text = (message.text or "").strip()
        if text.startswith("/start"):
            tokens = text.split(maxsplit=1)
            if len(tokens) == 2:
                payload = tokens[1].strip()
        if payload and _YOUTUBE_VIDEO_ID_RE.fullmatch(payload):
            url = f"https://www.youtube.com/watch?v={payload}"
            logger.info(
                "deep_link.start chat_id=%s video_id=%s source=browser_button",
                message.chat.id, payload,
            )
            if services.analytics is not None and not _is_allowed(message, services):
                user_id = _message_user_id(message)
                if user_id is not None:
                    services.analytics.record_first_start(user_id, "deep_link")
            await _enqueue_summary_job(message, url, services)
            return
        if payload:
            # Что-то пришло, но не video_id. Не молчим — пользователь скорее
            # всего открыл криво сформированную ссылку из старой версии
            # extension'а или playlist-страницу.
            await message.answer(t("start.bad_deeplink", lang))
            return
        if _is_allowed(message, services):
            await message.answer(t("start.allowlist", lang))
            return
        s = services.settings
        if services.analytics is not None:
            user_id = _message_user_id(message)
            if user_id is not None:
                services.analytics.record_first_start(user_id, "organic")
        await message.answer(
            t(
                "start.onboarding",
                lang,
                starter=s.quota_starter,
                weekly=s.quota_free_weekly,
                price=s.subscription_price_stars,
                monthly=s.quota_sub_monthly,
            ),
            parse_mode="HTML",
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        lang = _msg_lang(message, services)
        if not _has_access(message, services):
            await message.answer(t("access.closed", lang))
            return
        if not _is_owner(message, services):
            text = t("help.public", lang)
            if not _is_allowed(message, services):
                s = services.settings
                text += "\n\n" + t(
                    "help.external_extra",
                    lang,
                    starter=s.quota_starter,
                    weekly=s.quota_free_weekly,
                    price=s.subscription_price_stars,
                    monthly=s.quota_sub_monthly,
                )
            await message.answer(text)
            return
        await message.answer(
            "Команды:\n"
            "/last - последние 20 саммари\n\n"
            "/users - список пользователей\n\n"
            "/user_add - добавить пользователя (бот спросит id и имя)\n\n"
            "/user_remove - удалить пользователя (бот спросит id)\n\n"
            "/cancel - отменить начатый диалог (например, /user_add)\n\n"
            "/models - показать модели, доступные локальному LLM-серверу\n\n"
            "/model - показать модель, которую бот использует для summary\n\n"
            "/queue - показать очередь summary\n\n"
            "/stop - остановить текущую генерацию и очистить очередь\n\n"
            "/scan_now - вручную запустить сканер мониторинга или показать статус идущего скана\n\n"
            "/scan_stop - прервать запущенный мониторинговый скан\n\n"
            "/llm_mode - показать активный LLM-провайдер и режим (free/paid)\n\n"
            "/llm_paid - переключить OpenRouter между paid и free (тоггл)\n\n"
            "/cache_drop - убрать ролик из кэша саммари (аргумент — video_id или URL)\n\n"
            "/prompt_set - задать кастомный системный промпт для саммари (бот ждёт текст 5 мин)\n\n"
            "/prompt_show - показать текущий системный промпт\n\n"
            "/prompt_reset - вернуть системный промпт к дефолту"
        )

    @router.message(Command("last"))
    async def last_command(message: Message) -> None:
        lang = _msg_lang(message, services)
        if not _has_access(message, services):
            await message.answer(t("access.closed", lang))
            return
        user_id = _message_user_id(message)
        if user_id is None:
            await message.answer(
                "Не получилось определить твой Telegram-id. Перезапусти диалог через /start."
            )
            return
        if services.digests is None:
            await message.answer(
                "Архив саммари сейчас не активен (DigestStore не подключён)."
            )
            return
        entries = services.digests.list(user_id)
        # Тот же рендер, что и у закреплённого digest'а — список одинаков,
        # просто отправляется свежим сообщением (без pin'а).
        text = render_digest_html(entries)
        await message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @router.message(Command("limits"))
    async def limits(message: Message) -> None:
        lang = _msg_lang(message, services)
        if not _has_access(message, services):
            await message.answer(t("access.closed", lang))
            return
        user_id = _message_user_id(message)
        if user_id is None or services.quota is None or services.billing is None:
            await message.answer("Лимиты не настроены.")
            return
        if _is_allowed(message, services):
            await message.answer(t("limits.unlimited", lang))
            return
        s = services.settings
        verdict = services.quota.check(user_id)
        if verdict.is_subscriber:
            until = services.billing.subscription_until(user_id)
            until_text = datetime.datetime.fromtimestamp(until).strftime("%d.%m.%Y")
            await message.answer(
                t(
                    "limits.subscriber",
                    lang,
                    date=until_text,
                    remaining=verdict.remaining,
                    monthly=s.quota_sub_monthly,
                )
            )
            return
        if verdict.kind == "starter":
            await message.answer(
                t(
                    "limits.starter",
                    lang,
                    remaining=verdict.remaining,
                    starter=s.quota_starter,
                    weekly=s.quota_free_weekly,
                    price=s.subscription_price_stars,
                )
            )
            return
        if verdict.allowed:
            await message.answer(
                t(
                    "limits.weekly_available",
                    lang,
                    remaining=verdict.remaining,
                    weekly=s.quota_free_weekly,
                    price=s.subscription_price_stars,
                )
            )
        else:
            await message.answer(
                t(
                    "limits.weekly_exhausted",
                    lang,
                    price=s.subscription_price_stars,
                    monthly=s.quota_sub_monthly,
                )
            )

    @router.message(Command("subscribe"))
    async def subscribe(message: Message) -> None:
        lang = _msg_lang(message, services)
        if not _has_access(message, services):
            await message.answer(t("access.closed", lang))
            return
        if _is_allowed(message, services):
            await message.answer(t("subscribe.unlimited_already", lang))
            return
        await _send_subscription_invoice(message.chat.id, services, lang)

    @router.callback_query(F.data == "subscribe")
    async def subscribe_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message is not None:
            lang = _cb_lang(callback, services)
            await _send_subscription_invoice(callback.message.chat.id, services, lang)

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery) -> None:
        # Цифровая услуга, проверять нечего — подтверждаем всегда.
        await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def successful_payment(message: Message) -> None:
        payment = message.successful_payment
        user_id = _message_user_id(message)
        if user_id is None or services.billing is None:
            logger.error("billing.payment.no_user_or_store payload=%s", payment.invoice_payload)
            return
        until = _subscription_until_from_payment(payment)
        services.billing.activate_subscription(
            user_id, until_unix=until, charge_id=payment.telegram_payment_charge_id
        )
        if services.analytics is not None:
            is_first = bool(
                getattr(payment, "is_first_recurring", False)
                or not getattr(payment, "is_recurring", False)
            )
            services.analytics.record(
                user_id,
                "sub_activated" if is_first else "sub_renewed",
                detail=payment.telegram_payment_charge_id,
            )
        until_text = datetime.datetime.fromtimestamp(until).strftime("%d.%m.%Y")
        lang = _msg_lang(message, services)
        await message.answer(
            t(
                "subscribe.activated",
                lang,
                date=until_text,
                monthly=services.settings.quota_sub_monthly,
            )
        )

    @router.message(Command("paysupport"))
    async def paysupport(message: Message) -> None:
        # Обязательная команда по ToS Telegram для ботов, принимающих Stars.
        lang = _msg_lang(message, services)
        owner = services.settings.owner_user_id
        owner_label = t("paysupport.owner_label", lang)
        contact = f'<a href="tg://user?id={owner}">{owner_label}</a>' if owner else owner_label
        await message.answer(
            t("paysupport.text", lang, contact=contact),
            parse_mode="HTML",
        )

    @router.message(Command("refund"))
    async def refund(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /refund <user_id> [charge_id]")
            return
        try:
            target_user = int(parts[1])
        except ValueError:
            await message.answer("user_id должен быть числом.")
            return
        charge_id = parts[2] if len(parts) > 2 else (
            services.billing.last_charge_id(target_user) if services.billing else ""
        )
        if not charge_id:
            await message.answer("Не нашёл charge_id последнего платежа этого пользователя.")
            return
        try:
            await services.bot.refund_star_payment(
                user_id=target_user, telegram_payment_charge_id=charge_id
            )
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Возврат не прошёл: {exc}")
            return
        await message.answer(f"Возврат {charge_id} пользователю {target_user} выполнен.")

    @router.message(Command("users"))
    async def users(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        user_lines = []
        for user in services.users.list_users():
            label = f" — {user.name}" if user.name else ""
            marker = " (owner)" if services.users.is_owner(user.user_id) else ""
            user_lines.append(f"- {user.user_id}{label}{marker}")
        users_text = "\n".join(user_lines) if user_lines else "Список пуст."
        await message.answer(
            "Пользователи с доступом:\n"
            f"{users_text}\n\n"
            "Добавить: /user_add 123456789 Имя\n"
            "Удалить: /user_remove 123456789"
        )

    @router.message(Command("user_add"))
    async def user_add(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        # "/user_add 123 Имя" → сразу применяем.
        # "/user_add" без аргументов → запоминаем pending state и просим ввод
        # отдельным сообщением.
        parts = (message.text or "").split(maxsplit=1)
        raw_args = parts[1] if len(parts) > 1 else ""
        if not raw_args.strip():
            services.pending_admin_inputs[message.chat.id] = PendingAdminInput(
                action="user_add", started_at=time.time(),
            )
            await message.answer(
                "Введи Telegram-id и имя одной строкой — например:\n"
                "<code>123456789 Иван</code>\n\n"
                "Или /cancel чтобы отменить.",
                parse_mode="HTML",
            )
            return
        await _apply_user_add(message, raw_args, services)

    @router.message(Command("user_remove"))
    async def user_remove(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        parts = (message.text or "").split(maxsplit=1)
        raw_args = parts[1] if len(parts) > 1 else ""
        if not raw_args.strip():
            services.pending_admin_inputs[message.chat.id] = PendingAdminInput(
                action="user_remove", started_at=time.time(),
            )
            await message.answer(
                "Введи Telegram-id пользователя для удаления — например:\n"
                "<code>123456789</code>\n\n"
                "Или /cancel чтобы отменить.",
                parse_mode="HTML",
            )
            return
        await _apply_user_remove(message, raw_args, services)

    @router.message(Command("prompt_set"))
    async def prompt_set(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if services.system_prompts is None:
            await message.answer("System prompt store не подключён — команда недоступна.")
            return
        services.pending_admin_inputs[message.chat.id] = PendingAdminInput(
            action="prompt_set", started_at=time.time(),
        )
        current_chars = len(services.system_prompts.current())
        state = "кастомный" if services.system_prompts.is_custom() else "дефолт"
        await message.answer(
            f"Сейчас активен {state} системный промпт ({current_chars} симв).\n\n"
            "Пришли следующим сообщением новый текст системного промпта — "
            "он полностью заменит текущий. Один Telegram-сообщение = "
            "до 4096 символов. У тебя 5 минут.\n\n"
            "Или /cancel чтобы отменить, "
            "/prompt_show чтобы увидеть текущий, "
            "/prompt_reset чтобы вернуть дефолт.",
        )

    @router.message(Command("prompt_show"))
    async def prompt_show(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if services.system_prompts is None:
            await message.answer("System prompt store не подключён — команда недоступна.")
            return
        prompt_text = services.system_prompts.current()
        state = "кастомный" if services.system_prompts.is_custom() else "дефолт"
        header = f"Активен {state} системный промпт ({len(prompt_text)} симв):\n\n"
        # Telegram-лимит на сообщение — 4096 символов. Промпт может быть длиннее
        # (дефолт близок к 3000), плюс header. Если не влезает — режем и
        # предупреждаем, что показали превью.
        budget = MAX_TELEGRAM_MESSAGE_CHARS - len(header) - 32  # запас на "..."
        if len(prompt_text) <= budget:
            body = f"<pre>{escape_html(prompt_text)}</pre>"
            await message.answer(header + body, parse_mode="HTML")
            return
        preview = prompt_text[:budget].rstrip()
        body = f"<pre>{escape_html(preview)}</pre>\n\n… (обрезано, полный текст — {len(prompt_text)} симв)"
        await message.answer(header + body, parse_mode="HTML")

    @router.message(Command("cache_drop"))
    async def cache_drop(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if services.summary_cache is None:
            await message.answer("Кэш саммари не подключён.")
            return
        # "/cache_drop <id/url>" → применяем сразу.
        # "/cache_drop" без аргументов → 5-минутный pending: следующее сообщение
        # трактуем как id/URL (как у /user_add).
        parts = (message.text or "").split(maxsplit=1)
        raw_args = parts[1].strip() if len(parts) > 1 else ""
        if not raw_args:
            services.pending_admin_inputs[message.chat.id] = PendingAdminInput(
                action="cache_drop", started_at=time.time(),
            )
            await message.answer(
                "Пришли video_id или YouTube-URL ролика, который нужно убрать из кэша.\n\n"
                "Примеры:\n"
                "• <code>wy8ddeBYucY</code>\n"
                "• <code>https://youtu.be/wy8ddeBYucY</code>\n"
                "• <code>https://www.youtube.com/watch?v=wy8ddeBYucY</code>\n\n"
                "У тебя 5 минут. Отмена — /cancel.",
                parse_mode="HTML",
            )
            return
        await _apply_cache_drop(message, raw_args, services)

    @router.message(Command("prompt_reset"))
    async def prompt_reset(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if services.system_prompts is None:
            await message.answer("System prompt store не подключён — команда недоступна.")
            return
        # /prompt_reset может отменять любой висящий pending — иначе, если owner
        # набрал /prompt_set и передумал, ему пришлось бы дополнительно
        # /cancel'ить перед следующим запуском саммари.
        services.pending_admin_inputs.pop(message.chat.id, None)
        was_custom = services.system_prompts.reset()
        if was_custom:
            await message.answer(
                "Системный промпт сброшен на дефолт "
                f"({len(services.system_prompts.current())} симв). "
                "Следующее саммари уже пойдёт с ним."
            )
        else:
            await message.answer("Уже был дефолтный — ничего не менял.")

    @router.message(Command("cancel"))
    async def cancel(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if services.pending_admin_inputs.pop(message.chat.id, None) is not None:
            await message.answer("Окей, отменил. Никаких действий не сделано.")
        else:
            await message.answer("Сейчас нет активного диалога.")

    @router.message(Command("models"))
    async def models(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        try:
            models_list = await services.llm.list_models()
        except Exception as exc:
            await message.answer(f"Не удалось получить список моделей из {services.llm.provider_name}: {exc}")
            return

        if not models_list:
            await message.answer(f"{services.llm.provider_name} не вернул доступных моделей.")
            return

        lines = "\n".join(f"- {model}" for model in models_list[:30])
        lines = lines[:3500]
        suffix = "\n\nПоказаны первые 30 моделей." if len(models_list) > 30 else ""
        await message.answer(f"{services.llm.provider_name}: доступные модели\n{lines}{suffix}")

    @router.message(Command("model"))
    async def model(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        try:
            model_name = await services.llm.active_model()
        except Exception as exc:
            await message.answer(f"Не удалось определить активную модель {services.llm.provider_name}: {exc}")
            return

        await message.answer(
            f"Бот использует для summary:\n{services.llm.provider_name}: {model_name}"
        )

    @router.message(Command("queue"))
    async def queue(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        await message.answer(await _format_queue_status(services))

    @router.message(Command("stop"))
    async def stop(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        await _stop_summary_queue(message, services)

    @router.message(Command("scan_now"))
    async def scan_now(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if services.monitoring is None:
            await message.answer(
                "Мониторинг выключен. Включи MONITORING_ENABLED=true и перезапусти бота."
            )
            return

        existing = services.monitoring_scan_task
        if existing is not None and not existing.done():
            await message.answer(_format_scan_status(services))
            return

        asyncio.create_task(_run_manual_scan(message, services))

    @router.message(Command("llm_mode"))
    async def llm_mode(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        await message.answer(_format_llm_mode_status(services))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        """Owner-only: краткий отчёт по логам за последние 30 дней."""
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        # Дёргаем парсинг в отдельном thread'е — чтение 30 файлов не блокирует loop.
        await message.answer("Считаю статистику за 30 дней...")
        try:
            text = await asyncio.to_thread(_compute_stats_for_telegram, services, 30)
        except Exception as exc:
            logger.exception("stats.failed")
            await message.answer(f"Не удалось собрать статистику: {exc}")
            return
        job_counts = services.job_store.counts_since(30) if services.job_store else {}
        db_line = (
            f"Jobs за 30 дней (БД): ✅ {job_counts.get('done', 0)} · "
            f"❌ {job_counts.get('failed', 0)} · ⏹ {job_counts.get('cancelled', 0)}\n\n"
        )
        funnel_line = ""
        if services.analytics is not None:
            f = services.analytics.funnel(30)
            funnel_line = (
                "Воронка внешних пользователей (30 дн):\n"
                f"/start: {f['first_starts']} → первая генерация: {f['first_generations']} → "
                f"упёрлись в лимит: {f['quota_denied_users']} → подписка: {f['subs_activated']} "
                f"(продлений: {f['sub_renewals']})\n\n"
            )
        ytdlp_line = (
            f"yt-dlp сегодня: {services.youtube.ytdlp_today_count()} обращений "
            f"(мягкий лимит {services.settings.ytdlp_soft_daily_limit})\n\n"
        )
        await message.answer(
            db_line + funnel_line + ytdlp_line + text, parse_mode="HTML", disable_web_page_preview=True
        )

    @router.message(Command("llm_paid"))
    async def llm_paid(message: Message) -> None:
        """Toggle OpenRouter paid mode on/off.

        Off → free-chain (default).
        On → single paid model from OPENROUTER_MODEL_PAID.
        """
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return
        if not isinstance(services.llm, OpenRouterClient):
            await message.answer(
                "Команда работает только при LLM_PROVIDER=openrouter. "
                "Сейчас провайдер: " + services.llm.provider_name
            )
            return

        if services.llm.is_paid_mode():
            # Toggle OFF — back to free chain.
            services.llm.set_paid_mode(False)
            await message.answer(
                "Платный режим выключен. Вернулся на free-цепочку моделей.\n\n"
                + _format_llm_mode_status(services)
            )
            return

        # Toggle ON — switch to paid.
        if not services.llm.has_paid_model():
            await message.answer(
                "OPENROUTER_MODEL_PAID не настроен в .env. Прежде чем включать "
                "платный режим, добавь нужную модель и пересобери бот."
            )
            return
        services.llm.set_paid_mode(True)
        await message.answer(
            "Платный режим включён. Убедись, что на OpenRouter есть баланс.\n\n"
            + _format_llm_mode_status(services)
        )

    @router.message(Command("scan_stop"))
    async def scan_stop(message: Message) -> None:
        if not _is_owner(message, services):
            await _answer_owner_only(message, services)
            return

        existing = services.monitoring_scan_task
        if existing is None or existing.done():
            await message.answer("Сейчас никаких сканов не идёт.")
            return

        existing.cancel()
        await message.answer("Отменяю текущий скан...")

    @router.callback_query(F.data.startswith("download:"))
    async def download_audio_callback(callback: CallbackQuery) -> None:
        """Owner-only кнопка под саммари: скачать аудио YouTube-ролика и
        прислать его прямо в чат как reply к сообщению-саммари."""
        if not services.users.is_owner(callback.from_user.id if callback.from_user else None):
            await callback.answer("Эта кнопка доступна только владельцу.", show_alert=True)
            return
        video_id = (callback.data or "").removeprefix("download:").strip()
        if not video_id:
            await callback.answer("Неверный callback_data — нет video_id.", show_alert=True)
            return
        # Подтверждаем нажатие сразу — иначе у Telegram спиннер крутится 30 сек.
        await callback.answer("Готовлю аудио...")
        asyncio.create_task(_download_audio_to_chat(callback, video_id, services))

    @router.callback_query(F.data.startswith("transcript:"))
    async def transcript_callback(callback: CallbackQuery) -> None:
        """Кнопка под саммари: прислать сохранённый транскрипт (.md).

        Доступ — allowlist или активная подписка; для остальных alert
        с подсказкой /subscribe вместо файла.
        """
        video_id = (callback.data or "").split(":", 1)[1]
        user_id = callback.from_user.id if callback.from_user else None
        lang = _cb_lang(callback, services)
        allowed = user_id is not None and (
            services.users.is_allowed(user_id)
            or (services.billing is not None and services.billing.is_subscriber(user_id))
        )
        if not allowed:
            await callback.answer(
                t("transcript.subscribers_only", lang),
                show_alert=True,
            )
            return
        path = transcript_path(services.settings.bot_data_dir, video_id)
        if not path.exists():
            await callback.answer(
                t("transcript.not_saved", lang),
                show_alert=True,
            )
            return
        await callback.answer()
        filename = f"{video_id}.md"
        if services.summary_cache is not None:
            cached = services.summary_cache.get_any(video_id)
            if cached is not None:
                filename = pretty_transcript_filename(cached.channel_name, cached.title, video_id)
        if callback.message is not None:
            await services.bot.send_document(
                chat_id=callback.message.chat.id,
                document=FSInputFile(path, filename=filename),
                disable_notification=True,
            )

    # ─────────────────────── DISABLED: канал-публикация ───────────────────────
    # Фича «опубликовать в канал» временно отключена. Код callback'а и логики
    # публикации сохранён в виде комментариев, чтобы при необходимости можно
    # было быстро вернуть. См. _publish_to_channel ниже + ChannelPostsStore.
    #
    # @router.callback_query(F.data.startswith("publish:"))
    # async def publish_to_channel_callback(callback: CallbackQuery) -> None:
    #     ... (был owner-only с require TELEGRAM_PUBLISH_CHANNEL_ID,
    #     дёргал _publish_to_channel в asyncio.create_task)

    @router.message(F.text)
    async def text_message(message: Message) -> None:
        lang = _msg_lang(message, services)
        if not _has_access(message, services):
            await message.answer(t("access.closed", lang))
            return

        # Если в этот чат недавно ввели команду без аргументов («/user_add»,
        # «/user_remove»), мы запомнили action — следующий же текст owner'а
        # принимаем как параметры команды.
        pending = services.pending_admin_inputs.get(message.chat.id)
        if pending is not None and _is_owner(message, services):
            if time.time() - pending.started_at > PENDING_ADMIN_TIMEOUT_SEC:
                services.pending_admin_inputs.pop(message.chat.id, None)
                await message.answer(
                    "Прошлый диалог уже устарел (5 мин таймаут). Запусти команду заново."
                )
                return
            services.pending_admin_inputs.pop(message.chat.id, None)
            raw = (message.text or "").strip()
            if pending.action == "user_add":
                await _apply_user_add(message, raw, services)
            elif pending.action == "user_remove":
                await _apply_user_remove(message, raw, services)
            elif pending.action == "prompt_set":
                await _apply_prompt_set(message, message.text or "", services)
            elif pending.action == "cache_drop":
                await _apply_cache_drop(message, raw, services)
            return

        text = message.text or ""
        if text.strip().lower() in {"stop", "стоп"}:
            if not _is_owner(message, services):
                await _answer_owner_only(message, services)
                return
            await _stop_summary_queue(message, services)
            return

        url = extract_youtube_url(text)
        if url:
            kind = classify_youtube_url(url)
            if kind == "channel":
                if not _is_owner(message, services):
                    await message.answer(t("text.channel_only_video", lang))
                    return
                await _handle_channel_url(message, url, services)
                return
            if kind == "video":
                await _enqueue_summary_job(message, url, services)
                return
            await message.answer(t("text.unparseable_link", lang))
            return

        # В тексте есть http(s)-URL, но не от YouTube — отвечаем конкретно,
        # что это не YouTube-ссылка, а не общим "пришли ссылку на видео".
        foreign_url = extract_first_url(text)
        if foreign_url:
            try:
                host = urlparse(foreign_url).netloc or foreign_url
            except Exception:  # noqa: BLE001
                host = foreign_url
            await message.answer(
                t("text.not_youtube", lang, host=escape_html(host)),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        await message.answer(t("text.send_link_hint", lang))

    @router.message(Command("language"))
    async def language_command(message: Message) -> None:
        lang = _msg_lang(message, services)
        if not _has_access(message, services):
            await message.answer(t("access.closed", lang))
            return
        await message.answer(t("lang.prompt", lang), reply_markup=_language_keyboard())

    @router.callback_query(F.data.startswith("lang:"))
    async def language_callback(callback: CallbackQuery) -> None:
        code = (callback.data or "").removeprefix("lang:").strip()
        if code not in SUPPORTED_LANGS:
            await callback.answer("Invalid language code.", show_alert=True)
            return
        user_id = callback.from_user.id if callback.from_user else None
        if user_id is not None and services.user_langs is not None:
            services.user_langs.set(user_id, code, "manual")
        await callback.answer()
        name = LANG_NATIVE_NAMES[code]
        if callback.message is not None:
            await callback.message.edit_text(t("lang.changed", code, name=name))

    return router


SCAN_PROGRESS_THROTTLE_SEC = 1.5


def _compute_stats_for_telegram(services: Services, days: int) -> str:
    """Aggregate logs + render compact HTML for /stats command.

    Synchronous function — caller wraps in ``asyncio.to_thread`` because
    reading + parsing all rotated archives can take a few hundred ms.
    """
    logs_dir = services.settings.bot_data_dir / "logs"
    since = datetime.datetime.now() - datetime.timedelta(days=days)
    events = log_analytics.iter_events(logs_dir, since=since)
    stats = log_analytics.aggregate(events)

    # Резолв chat_id → display name (через UserStore.list_users()).
    # Кэшим один раз перед агрегацией — не бить get'ом по каждому event'у.
    name_by_id: dict[int, str] = {}
    try:
        for u in services.users.list_users():
            if u.name:
                name_by_id[u.user_id] = u.name
    except Exception as exc:  # noqa: BLE001
        logger.warning("stats.user_resolver_failed error=%s", exc)

    def _name_resolver(chat_id: str) -> str | None:
        try:
            return name_by_id.get(int(chat_id))
        except (TypeError, ValueError):
            return None

    return log_analytics.format_telegram(
        stats,
        name_resolver=_name_resolver,
        summary_cache=services.summary_cache,
    )
def _format_llm_mode_status(services: Services) -> str:
    """Render the active LLM provider/mode for /llm_mode."""
    llm = services.llm
    provider = llm.provider_name
    if not isinstance(llm, OpenRouterClient):
        return (
            f"Провайдер: {provider}\n"
            "Переключение режимов /llm_paid /llm_free доступно только для OpenRouter."
        )

    mode = "платный" if llm.is_paid_mode() else "бесплатный (free)"
    chain = llm.current_chain()
    chain_lines = "\n".join(f"  {i+1}. {m}" for i, m in enumerate(chain)) or "  (пусто)"
    snap = llm.budget.snapshot()
    settings = services.settings

    lines = [
        f"Провайдер: {provider}",
        f"Режим: {mode}",
        "",
        "Модели в порядке приоритета:",
        chain_lines,
        "",
    ]

    if llm.is_paid_mode():
        lines.append(
            "Платная модель — без fallback'а. Лимиты:"
        )
        if settings.openrouter_daily_budget_usd > 0:
            lines.append(
                f"  Дневной бюджет: ${snap.get('spent_usd', 0.0):.4f} / "
                f"${settings.openrouter_daily_budget_usd:.2f}"
            )
        else:
            lines.append("  Дневной бюджет: отключён ($0 = без лимита)")
    else:
        passes = settings.openrouter_fallback_retry_passes + 1
        delay = settings.openrouter_fallback_retry_delay_sec
        lines.append(
            f"Fallback: {passes} прохода × {len(chain)} моделей "
            f"(задержка между проходами {delay}с). Лимиты:"
        )

    if settings.openrouter_daily_request_limit > 0:
        lines.append(
            f"  Запросов сегодня: {snap.get('request_count', 0)} / "
            f"{settings.openrouter_daily_request_limit}"
        )
    else:
        lines.append("  Дневной лимит запросов: отключён")

    lines.append("")
    lines.append("Команда переключения: /llm_paid (тоггл paid ↔ free)")
    return "\n".join(lines)
def _format_scan_status(services: Services) -> str:
    """Render a fresh status snapshot for /scan_now invoked while a scan is already running."""
    snap = services.monitoring_scan_progress
    if snap is None:
        # Task created but progress callback hasn't fired yet (very early stage).
        return "Скан запущен, подбираю первый канал..."

    if snap.current_channel is None:
        return (
            f"Скан почти завершён: {snap.channels_done}/{snap.channels_total}. "
            f"В очереди summary: {snap.enqueued_total}."
        )
    label = snap.current_channel.channel_name or snap.current_channel.channel_id
    return (
        f"Скан уже идёт.\n"
        f"Прогресс: {snap.channels_done}/{snap.channels_total}\n"
        f"Сейчас: {label}\n"
        f"В очереди summary: {snap.enqueued_total}\n\n"
        f"Чтобы прервать — /scan_stop."
    )
async def _run_manual_scan(message: Message, services: Services) -> None:
    """Hand-trigger a monitoring scan and report progress to Telegram.

    Fires from /scan_now. The scan itself takes the same lock the daily
    scheduler uses, so a parallel scheduled tick won't double-run.

    Renders progress into a single status message that is edited as each
    channel finishes; edits are throttled so we don't hit Telegram's
    rate limit on per-message edits.

    Cancellation: if the wrapping asyncio.Task is cancelled (e.g. via
    /scan_stop), we replace the status text and bail out. The
    MonitoringService unwinds httpx/lock state on its own.
    """
    if services.monitoring is None:
        return

    rules = services.monitoring.config.rules
    channels_count = len(rules.channels)
    if channels_count == 0:
        await message.answer(
            "В data/monitoring.yaml не задан ни один канал. Добавь хэндлы и повтори."
        )
        return

    notice = await message.answer(
        f"Запускаю ручной скан мониторинга по {channels_count} каналам. "
        f"Это может занять пару минут."
    )

    last_edit_at = 0.0
    last_text = ""

    async def _edit_status(text: str, force: bool = False) -> None:
        nonlocal last_edit_at, last_text
        now = time.monotonic()
        if not force and now - last_edit_at < SCAN_PROGRESS_THROTTLE_SEC:
            return
        if text == last_text:
            return
        try:
            await notice.edit_text(text)
            last_edit_at = now
            last_text = text
        except Exception:
            # Telegram could throw "message is not modified" or rate-limit,
            # both are non-fatal for the scan itself.
            pass

    async def _on_progress(snapshot: ScanProgress) -> None:
        services.monitoring_scan_progress = snapshot
        if snapshot.current_channel is not None:
            current_label = (
                snapshot.current_channel.channel_name
                or snapshot.current_channel.channel_id
            )
            text = (
                f"Сканирую мониторинг: {snapshot.channels_done}/{snapshot.channels_total}\n"
                f"Сейчас: {current_label}\n"
                f"В очереди summary: {snapshot.enqueued_total}"
            )
            await _edit_status(text)

    services.monitoring_scan_task = asyncio.current_task()
    services.monitoring_scan_progress = None
    services.monitoring_scan_started_at = time.monotonic()
    async def _check_llm() -> tuple[bool, str]:
        return await health_check_with_reason(services.llm)

    try:
        try:
            enqueued = await services.monitoring.run_scan(
                progress=_on_progress,
                llm_check=_check_llm,
            )
        except asyncio.CancelledError:
            snap = services.monitoring_scan_progress
            done = snap.channels_done if snap is not None else 0
            enq = snap.enqueued_total if snap is not None else 0
            await _edit_status(
                f"Скан остановлен. Прошёл каналов: {done}/{channels_count}. "
                f"В очередь успели добавить: {enq}.",
                force=True,
            )
            raise
        except Exception as exc:
            logger.exception("monitoring.scan_now.failed")
            await _edit_status(f"Скан упал: {exc}", force=True)
            return

        if enqueued == 0:
            final = (
                f"Скан завершён ({channels_count}/{channels_count}). "
                f"Новых подходящих видео не найдено: либо уже прошли генерацию, "
                f"либо не прошли фильтры."
            )
        else:
            final = (
                f"Скан завершён ({channels_count}/{channels_count}). "
                f"В очередь summary добавлено: {enqueued}. "
                f"Жди уведомлений по мере готовности."
            )
        await _edit_status(final, force=True)
    finally:
        services.monitoring_scan_task = None
        services.monitoring_scan_progress = None
        services.monitoring_scan_started_at = None
async def _handle_channel_url(message: Message, url: str, services: Services) -> None:
    if services.monitoring is None:
        await message.answer(
            "Мониторинг каналов выключен. Включи MONITORING_ENABLED=true и перезапусти бота."
        )
        return

    notice = await message.answer("Проверяю канал и добавляю в мониторинг...")
    try:
        channel, added = await services.monitoring.add_channel_by_url(url)
    except Exception as exc:
        logger.exception("monitoring.add_channel.failed url=%s", url)
        await notice.edit_text(f"Не получилось добавить канал. Причина: {exc}")
        return

    label = channel.channel_name or channel.channel_id
    if added:
        text = (
            f"Канал добавлен в мониторинг: {label}.\n"
            f"Новые видео буду проверять раз в сутки."
        )
    else:
        text = f"Канал уже в мониторинге: {label}."
    await notice.edit_text(text)
async def _answer_owner_only(message: Message, services: Services) -> None:
    if not _is_allowed(message, services):
        await message.answer(t("access.closed", _msg_lang(message, services)))
        return
    await message.answer("Эта команда доступна только владельцу бота.")
async def _apply_user_add(message: Message, raw_args: str, services: Services) -> None:
    """Parse a "<user_id> [name...]" string and add user. Used both inline
    (``/user_add 123 Иван``) and via two-step pending dialog."""
    parts = raw_args.strip().split(maxsplit=1)
    if not parts:
        await message.answer("Не понял ввод. Нужен Telegram-id и имя.")
        return
    try:
        user_id = int(parts[0])
    except ValueError:
        await message.answer("Telegram user id должен быть числом.")
        return

    name = parts[1] if len(parts) > 1 else ""
    added = services.users.add_user(user_id, name)
    if added:
        await message.answer(f"Пользователь {user_id} добавлен.")
    else:
        await message.answer(f"Пользователь {user_id} уже был в списке. Данные обновлены.")
async def _apply_user_remove(message: Message, raw_args: str, services: Services) -> None:
    """Parse a single user_id and remove that user. Used inline + pending dialog."""
    cleaned = raw_args.strip().split()
    if not cleaned:
        await message.answer("Не понял ввод. Нужен Telegram-id.")
        return
    try:
        user_id = int(cleaned[0])
    except ValueError:
        await message.answer("Telegram user id должен быть числом.")
        return

    try:
        removed = services.users.remove_user(user_id)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    if removed:
        await message.answer(f"Пользователь {user_id} удалён.")
    else:
        await message.answer(f"Пользователя {user_id} нет в списке.")
async def _apply_cache_drop(message: Message, raw_args: str, services: Services) -> None:
    """Parse a video_id or YouTube URL and drop the corresponding cache entry.

    Общий helper для двух путей: инлайн-аргументы у ``/cache_drop <id/url>`` и
    двухшаговый pending-диалог.
    """
    if services.summary_cache is None:
        await message.answer("Кэш саммари не подключён.")
        return
    raw = raw_args.strip()
    if not raw:
        await message.answer(
            "Пустой ввод. Пришли video_id или URL, либо /cancel."
        )
        return
    try:
        video_id = extract_video_id(raw)
    except Exception:
        # extract_video_id умеет и «голый» 11-символьный id, и разные формы URL.
        # Если он не разобрал — используем ввод как есть, дальше проверим формат.
        video_id = raw
    if not _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id or ""):
        await message.answer(
            f"«{escape_html(raw)}» не похоже на video_id или YouTube-URL. "
            "video_id — ровно 11 символов из [A-Za-z0-9_-].",
            parse_mode="HTML",
        )
        return
    removed = services.summary_cache.delete(video_id)
    transcript_path(services.settings.bot_data_dir, video_id).unlink(missing_ok=True)
    if removed:
        await message.answer(
            f"Убрал <code>{video_id}</code> из кэша. Следующая ссылка на этот ролик "
            "пойдёт через свежую генерацию.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"<code>{video_id}</code> в кэше не найден — уже отсутствует или id неверный.",
            parse_mode="HTML",
        )
async def _apply_prompt_set(message: Message, raw_text: str, services: Services) -> None:
    """Save the follow-up message text as the new custom system prompt.

    Called from the pending-dialog branch of the text handler when the owner
    types ``/prompt_set`` (no args) and then sends the prompt in the next
    message. We preserve internal newlines/whitespace of the prompt — only
    trim the outer edges — and warn if the message looks empty or is another
    command the user might have fired by accident.
    """
    if services.system_prompts is None:
        await message.answer("System prompt store не подключён — нечего сохранять.")
        return

    prompt_text = raw_text.strip()
    if not prompt_text:
        await message.answer(
            "Пустой текст — не сохраняю. Запусти /prompt_set заново и пришли текст промпта."
        )
        return
    # Если owner случайно прислал команду вместо текста промпта — не хотим
    # запечь "/models" как system prompt.
    if prompt_text.startswith("/"):
        await message.answer(
            "Первый символ — «/», похоже на команду, а не на промпт. "
            "Не сохраняю. Запусти /prompt_set заново и пришли именно текст промпта."
        )
        return

    if len(prompt_text) > MAX_SYSTEM_PROMPT_CHARS:
        await message.answer(
            f"Промпт слишком длинный: {len(prompt_text)} символов при лимите "
            f"{MAX_SYSTEM_PROMPT_CHARS}. Такой промпт съест контекст модели и "
            "испортит все саммари. Сократи и пришли ещё раз."
        )
        return

    services.system_prompts.set(prompt_text)
    await message.answer(
        f"Сохранил новый системный промпт ({len(prompt_text)} симв). "
        "Следующее саммари уйдёт уже с ним. "
        "Откатить — /prompt_reset."
    )
def _is_allowed(message: Message, services: Services) -> bool:
    return services.users.is_allowed(_message_user_id(message))
def _is_owner(message: Message, services: Services) -> bool:
    return services.users.is_owner(_message_user_id(message))
def _has_access(message: Message, services: Services) -> bool:
    """Allowlist — всегда да; внешние — только при PUBLIC_MODE."""
    return _is_allowed(message, services) or services.settings.public_mode
