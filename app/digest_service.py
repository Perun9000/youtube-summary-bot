"""Per-user pinned digest of recent summaries.

Keeps a rolling list of the N most-recent summaries each user has requested
(or had delivered to them via monitoring, in the owner's case) and maintains
a single pinned message in their private chat with the bot. Every time a new
summary lands — fresh or cache-hit — we:

  1. Append (or move-to-top, if already present) the entry into that user's
     digest.
  2. Render the digest as HTML.
  3. If a pinned message already exists for this user → ``edit_message_text``
     keeps the same pin in place. Otherwise: send a new message, pin it,
     persist the message_id.

**Important constraint:** Telegram does not provide deep-links to messages
inside 1-on-1 chats with a bot (``t.me/c/<chat>/<msg>`` works only in groups
and channels). So the digest's hyperlinks point to **Telegra.ph** — every
summary already has a Telegraph page with the full body, and the user gets
to the same content either way.

Persistence layout:
  ``data/digests.json``       — ``{user_id: [DigestEntry, ...]}``
  ``data/digest_pins.json``   — ``{user_id: {chat_id, message_id}}``

Both files are written atomically via tmp + replace; concurrent access is
guarded by a process-local ``threading.Lock``. Async network calls (the
Telegram bot API ones) run outside the lock to avoid holding it across IO.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.utils import escape_html

if TYPE_CHECKING:
    from aiogram import Bot


logger = logging.getLogger(__name__)


# Сколько последних саммари держим в дайджесте на пользователя. 20 строк
# по ~80 символов = ~1.6K — с большим запасом помещается в одно Telegram
# сообщение (лимит 4096). Старшее всё ещё видно в архиве кэша через /stats,
# просто не в закрепе.
DIGEST_LIMIT = 20

# Telegram-лимит ~4096 символов на сообщение. Берём 4000 с запасом на
# хвостовой троеточие/служебные кусочки, как и в bot_handlers._fit_telegram_message.
MAX_DIGEST_CHARS = 4000


@dataclass
class DigestEntry:
    """Одна запись дайджеста — round-trip serialisable to JSON."""

    video_id: str
    title: str
    telegraph_url: str
    channel_name: str = ""
    created_at_unix: float = 0.0


class DigestStore:
    """Per-user digest list + pinned-message tracking, JSON-persisted."""

    def __init__(self, digests_path: Path, pins_path: Path, limit: int = DIGEST_LIMIT) -> None:
        self._digests_path = digests_path
        self._pins_path = pins_path
        self._limit = limit
        self._lock = threading.Lock()
        # user_id -> list[DigestEntry], в порядке most-recent-first.
        self._digests: dict[int, list[DigestEntry]] = {}
        # user_id -> {"chat_id": int, "message_id": int}.
        self._pins: dict[int, dict[str, int]] = {}
        # Защищаем pin-update от гонки: если на одного пользователя за секунду
        # прилетят два саммари (cache-hit + scheduled), параллельные edit'ы
        # друг друга затрут, и Telegram нам отдаст 400 на стороне второго.
        # Lock per-user, ленивая инициализация.
        self._pin_update_locks: dict[int, asyncio.Lock] = {}
        self._load()

    # ── persistence ───────────────────────────────────────────────────

    def _load(self) -> None:
        digests = self._load_json(self._digests_path)
        if isinstance(digests, dict):
            parsed: dict[int, list[DigestEntry]] = {}
            for raw_uid, raw_entries in digests.items():
                try:
                    user_id = int(raw_uid)
                except (TypeError, ValueError):
                    continue
                if not isinstance(raw_entries, list):
                    continue
                entries: list[DigestEntry] = []
                for body in raw_entries:
                    if not isinstance(body, dict):
                        continue
                    try:
                        entries.append(DigestEntry(**body))
                    except (TypeError, KeyError) as exc:
                        logger.warning(
                            "digests.skip_entry user_id=%s error=%s", user_id, exc
                        )
                parsed[user_id] = entries[: self._limit]
            self._digests = parsed

        pins = self._load_json(self._pins_path)
        if isinstance(pins, dict):
            parsed_pins: dict[int, dict[str, int]] = {}
            for raw_uid, body in pins.items():
                try:
                    user_id = int(raw_uid)
                except (TypeError, ValueError):
                    continue
                if not isinstance(body, dict):
                    continue
                try:
                    parsed_pins[user_id] = {
                        "chat_id": int(body["chat_id"]),
                        "message_id": int(body["message_id"]),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
            self._pins = parsed_pins

        logger.info(
            "digests.loaded users=%s pins=%s digests_path=%s pins_path=%s",
            len(self._digests), len(self._pins),
            self._digests_path, self._pins_path,
        )

    def _load_json(self, path: Path):
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("digests.load_failed path=%s error=%s", path, exc)
            return None

    def _save_digests_locked(self) -> None:
        payload = {
            str(uid): [asdict(e) for e in entries]
            for uid, entries in self._digests.items()
        }
        self._atomic_write(self._digests_path, payload)

    def _save_pins_locked(self) -> None:
        payload = {str(uid): dict(body) for uid, body in self._pins.items()}
        self._atomic_write(self._pins_path, payload)

    def _atomic_write(self, path: Path, payload) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("digests.save_failed path=%s error=%s", path, exc)

    # ── digest list ───────────────────────────────────────────────────

    def add(self, user_id: int, entry: DigestEntry) -> list[DigestEntry]:
        """Insert entry at the top of user's digest, dedup'd by ``video_id``.

        If the user already has this video — old position is dropped, new
        version (with potentially refreshed title/created_at) is placed at top.
        Returns the post-mutation list (caller renders it).
        """
        with self._lock:
            existing = self._digests.get(user_id, [])
            filtered = [e for e in existing if e.video_id != entry.video_id]
            filtered.insert(0, entry)
            filtered = filtered[: self._limit]
            self._digests[user_id] = filtered
            self._save_digests_locked()
            return list(filtered)

    def list(self, user_id: int) -> list[DigestEntry]:
        with self._lock:
            return list(self._digests.get(user_id, []))

    # ── pin tracking ──────────────────────────────────────────────────

    def get_pin(self, user_id: int) -> tuple[int, int] | None:
        with self._lock:
            body = self._pins.get(user_id)
            if body is None:
                return None
            return body["chat_id"], body["message_id"]

    def set_pin(self, user_id: int, chat_id: int, message_id: int) -> None:
        with self._lock:
            self._pins[user_id] = {"chat_id": chat_id, "message_id": message_id}
            self._save_pins_locked()

    def clear_pin(self, user_id: int) -> None:
        with self._lock:
            if user_id in self._pins:
                del self._pins[user_id]
                self._save_pins_locked()

    def _get_or_create_pin_lock(self, user_id: int) -> asyncio.Lock:
        with self._lock:
            lock = self._pin_update_locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._pin_update_locks[user_id] = lock
            return lock


# ──────────────────────────── rendering ────────────────────────────

def render_digest_html(entries: list[DigestEntry]) -> str:
    """Render the digest body for Telegram (HTML parse-mode).

    Layout (top → bottom = oldest → newest, чтобы свежее саммари визуально
    оказывалось ближе к низу сообщения, по логике чата):

        📚 <b>Последние саммари</b>

        • <a href="https://telegra.ph/...">Старый заголовок</a> · Канал
        • <a href="...">…</a>
        • <a href="https://telegra.ph/...">Самый свежий заголовок</a>

    Each entry — буллит + title-как-гиперссылка на Telegra.ph + опциональный
    суффикс « · Канал ».

    Безопасность относительно Telegram'овского лимита (4096 char):
    идём по списку **сверху** (новейшие первыми, как хранит DigestStore)
    и складываем строки, пока влезает. Когда упёрлись в бюджет — молча
    стопаемся. В видимый набор всегда попадают самые свежие записи,
    обрезаются самые старые (без какого-либо «… ещё N» индикатора —
    пользователь просто видит ровно столько роликов, сколько помещается).
    Затем переворачиваем порядок (oldest at top, newest at bottom).
    HTML всегда валиден (каждая строка — целое ``<a>…</a>``), 400-ка от
    Telegram'а нам не грозит даже на длинных заголовках.
    """
    if not entries:
        return (
            "📚 <b>Последние саммари</b>\n\n"
            "<i>Пока пусто. Пришли YouTube-ссылку — и здесь появится первая запись.</i>"
        )

    head = "📚 <b>Последние саммари</b>"
    budget = MAX_DIGEST_CHARS

    included: list[str] = []
    # +2 — head + пустая строка после head.
    used = len(head) + 2

    for e in entries:  # хранилище отдаёт newest-first
        title = escape_html(e.title or e.video_id)
        url = escape_html(e.telegraph_url)
        channel = (e.channel_name or "").strip()
        suffix = f" · {escape_html(channel)}" if channel else ""
        line = f"• <a href=\"{url}\">{title}</a>{suffix}"
        cost = len(line) + 1  # +1 за разделитель «\n»
        if used + cost > budget:
            break
        included.append(line)
        used += cost

    # Переворачиваем: старые наверху, новые внизу.
    included.reverse()
    return "\n".join([head, "", *included])


# ──────────────────────────── pin update ────────────────────────────

async def update_pin_for_user(
    store: DigestStore,
    bot: "Bot",
    user_id: int,
    chat_id: int,
    entry: DigestEntry,
) -> None:
    """Append (or move-to-top) entry, then refresh the pinned digest.

    Failure modes are isolated: if Telegram returns an error (chat deleted,
    bot blocked, message deleted etc.) we log + try to recover by re-sending,
    but we never raise — the caller is the summary delivery path, and we
    don't want a digest-update failure to fail the delivery.
    """
    try:
        entries = store.add(user_id, entry)
    except Exception:
        logger.exception("digests.add_failed user_id=%s video_id=%s", user_id, entry.video_id)
        return

    # Per-user lock — гарантирует, что одновременно прилетевшие два саммари
    # не будут параллельно слать edit/pin одной и той же шапки.
    lock = store._get_or_create_pin_lock(user_id)
    async with lock:
        text = render_digest_html(entries)
        existing_pin = store.get_pin(user_id)
        if existing_pin is not None:
            pin_chat_id, pin_message_id = existing_pin
            if pin_chat_id == chat_id:
                # Тот же чат — пробуем редактировать существующий закреп.
                edited = await _try_edit(bot, chat_id, pin_message_id, text)
                if edited:
                    return
                # edit не получился — pin'а уже нет или его удалили.
                # Сбрасываем metadata и фолбэк-ом отправляем новый.
                store.clear_pin(user_id)
            else:
                # Чат сменился (теоретически невозможно для private — но
                # обработаем). Сбрасываем старый pin и заводим новый.
                store.clear_pin(user_id)

        new_message_id = await _send_and_pin(bot, chat_id, text)
        if new_message_id is not None:
            store.set_pin(user_id, chat_id, new_message_id)


async def _try_edit(bot: "Bot", chat_id: int, message_id: int, text: str) -> bool:
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # «message is not modified» — успех (контент уже актуален).
        if "not modified" in str(exc).lower():
            return True
        logger.warning(
            "digests.edit_failed chat_id=%s message_id=%s error=%s",
            chat_id, message_id, exc,
        )
        return False


async def _send_and_pin(bot: "Bot", chat_id: int, text: str) -> int | None:
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("digests.send_failed chat_id=%s error=%s", chat_id, exc)
        return None
    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception as exc:  # noqa: BLE001
        # send удалось, pin — нет. Сообщение всё равно сохраняем как
        # «текущий дайджест», просто незакреплённое. На следующем тике
        # попробуем переzакрепить новое.
        logger.warning(
            "digests.pin_failed chat_id=%s message_id=%s error=%s",
            chat_id, msg.message_id, exc,
        )
    return msg.message_id
