"""Per-user digest of recent summaries (data for /last).

Keeps a rolling list of the N most-recent summaries each user has requested
(or had delivered via monitoring, in the owner's case). The list is served
on demand by the /last command.

История: до 2026-07-28 модуль ещё и поддерживал закреплённое сообщение-дайджест
в чате (edit/pin при каждом саммари) — удалено по решению владельца, остались
только данные. Таблица ``digest_pins`` в схеме больше не используется.

**Important constraint:** Telegram does not provide deep-links to messages
inside 1-on-1 chats with a bot, so digest hyperlinks point to **Telegra.ph**.

Persistence: SQLite table ``digests``. Legacy JSON (``data/digests.json``)
migrates in on first boot, then is renamed to ``*.migrated``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from app.db import Database, retire_legacy_json
from app.utils import escape_html


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
    """Per-user digest list поверх SQLite (данные для /last)."""

    def __init__(
        self,
        db: Database,
        limit: int = DIGEST_LIMIT,
        legacy_digests_path: Path | None = None,
    ) -> None:
        self._db = db
        self._limit = limit
        if legacy_digests_path is not None:
            self._migrate_digests(legacy_digests_path)

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
