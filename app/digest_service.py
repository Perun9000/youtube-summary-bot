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

Persistence: SQLite tables ``digests`` and ``digest_pins`` (see ``app.db``).
Legacy JSON files (``data/digests.json`` / ``data/digest_pins.json``) are
migrated in on first boot, then renamed to ``*.migrated``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.db import Database, retire_legacy_json
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
    """Per-user digest list + pinned-message tracking поверх SQLite."""

    def __init__(
        self,
        db: Database,
        limit: int = DIGEST_LIMIT,
        legacy_digests_path: Path | None = None,
        legacy_pins_path: Path | None = None,
    ) -> None:
        self._db = db
        self._limit = limit
        self._pin_update_locks: dict[int, asyncio.Lock] = {}
        self._pin_locks_guard = threading.Lock()
        if legacy_digests_path is not None:
            self._migrate_digests(legacy_digests_path)
        if legacy_pins_path is not None:
            self._migrate_pins(legacy_pins_path)

    def _migrate_digests(self, path: Path) -> None:
        if not path.exists():
            return
        row = self._db.query_one("SELECT COUNT(*) AS n FROM digests")
        if row and int(row["n"]) > 0:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("digests.migrate.load_failed path=%s", path)
            return
        if isinstance(raw, dict):
            for raw_uid, raw_entries in raw.items():
                try:
                    user_id = int(raw_uid)
                except (TypeError, ValueError):
                    continue
                if not isinstance(raw_entries, list):
                    continue
                for body in raw_entries:
                    if isinstance(body, dict):
                        try:
                            self._insert(user_id, DigestEntry(**body))
                        except (TypeError, KeyError):
                            continue
        retire_legacy_json(path)

    def _migrate_pins(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("digests.migrate.pins_load_failed path=%s", path)
            return
        if isinstance(raw, dict):
            for raw_uid, body in raw.items():
                try:
                    self.set_pin(int(raw_uid), int(body["chat_id"]), int(body["message_id"]))
                except (TypeError, KeyError, ValueError):
                    continue
        retire_legacy_json(path)

    def _insert(self, user_id: int, entry: DigestEntry) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO digests(user_id, video_id, title, telegraph_url, channel_name, created_at_unix) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, entry.video_id, entry.title, entry.telegraph_url, entry.channel_name, entry.created_at_unix),
        )

    def add(self, user_id: int, entry: DigestEntry) -> list[DigestEntry]:
        self._insert(user_id, entry)
        # Подрезаем хвост за limit — старые записи наружу не отдаются, так что
        # можно чистить сразу на записи. rowid DESC — tie-breaker при равных
        # created_at_unix (порядок вставки).
        self._db.execute(
            "DELETE FROM digests WHERE user_id = ? AND video_id NOT IN ("
            "  SELECT video_id FROM digests WHERE user_id = ? "
            "  ORDER BY created_at_unix DESC, rowid DESC LIMIT ?)",
            (user_id, user_id, self._limit),
        )
        return self.list(user_id)

    def list(self, user_id: int) -> list[DigestEntry]:
        rows = self._db.query(
            "SELECT video_id, title, telegraph_url, channel_name, created_at_unix "
            "FROM digests WHERE user_id = ? ORDER BY created_at_unix DESC, rowid DESC LIMIT ?",
            (user_id, self._limit),
        )
        return [
            DigestEntry(
                video_id=r["video_id"], title=r["title"], telegraph_url=r["telegraph_url"],
                channel_name=r["channel_name"], created_at_unix=r["created_at_unix"],
            )
            for r in rows
        ]

    def get_pin(self, user_id: int) -> tuple[int, int] | None:
        row = self._db.query_one("SELECT chat_id, message_id FROM digest_pins WHERE user_id = ?", (user_id,))
        return (row["chat_id"], row["message_id"]) if row else None

    def set_pin(self, user_id: int, chat_id: int, message_id: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO digest_pins(user_id, chat_id, message_id) VALUES (?, ?, ?)",
            (user_id, chat_id, message_id),
        )

    def clear_pin(self, user_id: int) -> None:
        self._db.execute("DELETE FROM digest_pins WHERE user_id = ?", (user_id,))

    def _get_or_create_pin_lock(self, user_id: int) -> asyncio.Lock:
        with self._pin_locks_guard:
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
