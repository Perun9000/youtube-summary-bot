"""Единая SQLite-база бота.

Все store-классы (пользователи, кэш саммари, дайджесты, состояние мониторинга,
бюджет OpenRouter, персистентная очередь) живут в одном файле ``data/bot.db``.
Дизайн-решения:

- stdlib ``sqlite3``, синхронный API. Каждый запрос — миллисекунды, объёмы
  крошечные; тащить aiosqlite и делать все call-sites асинхронными незачем.
- Одно соединение на процесс, ``check_same_thread=False`` + process-local
  ``threading.Lock`` вокруг каждого запроса: и event loop, и to_thread-вызовы
  ходят через один сериализованный вход. Это убирает гонки, которые раньше
  были возможны между JSON-файлами.
- WAL — чтобы редкие конкурирующие чтения не ждали писателя.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path


logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS summary_cache (
    video_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at_unix REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
    user_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    telegraph_url TEXT NOT NULL DEFAULT '',
    channel_name TEXT NOT NULL DEFAULT '',
    created_at_unix REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, video_id)
);
CREATE TABLE IF NOT EXISTS digest_pins (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS monitoring_seen (
    channel_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    added_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, video_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    scheduled INTEGER NOT NULL DEFAULT 0,
    disable_notification INTEGER NOT NULL DEFAULT 0,
    title_hint TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    run_after REAL,
    lang TEXT NOT NULL DEFAULT 'ru'
);
CREATE TABLE IF NOT EXISTS morning_digest_items (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    channel_name TEXT NOT NULL DEFAULT '',
    telegraph_url TEXT NOT NULL DEFAULT '',
    overview TEXT NOT NULL DEFAULT '',
    tags_line TEXT NOT NULL DEFAULT '',
    duration_sec REAL NOT NULL DEFAULT 0,
    created_at_unix REAL NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    until_unix REAL NOT NULL DEFAULT 0,
    last_charge_id TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    video_id TEXT NOT NULL DEFAULT '',
    weight INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'free',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_events(user_id, created_at);
CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analytics_user_event ON analytics_events(user_id, event);
CREATE INDEX IF NOT EXISTS idx_analytics_event_time ON analytics_events(event, created_at);
CREATE TABLE IF NOT EXISTS user_langs (
    user_id INTEGER PRIMARY KEY,
    lang TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    updated_at REAL NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            # Миграция для баз, созданных до появления run_after (премьеры):
            # CREATE IF NOT EXISTS новые колонки не добавляет.
            try:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN run_after REAL")
                logger.info("db.migrate jobs.run_after added")
            except sqlite3.OperationalError:
                pass  # колонка уже есть
            # Миграция для баз, созданных до появления SummaryJob.lang: язык
            # задачи едет вместе с job'ом (см. app/services_container.py).
            try:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN lang TEXT NOT NULL DEFAULT 'ru'")
                logger.info("db.migrate jobs.lang added")
            except sqlite3.OperationalError:
                pass  # колонка уже есть
            self._conn.commit()
        logger.info("db.boot path=%s", path)

    @property
    def path(self) -> Path:
        return self._path

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def executemany(self, sql: str, seq) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def execute_returning_rowid(self, sql: str, params: tuple = ()) -> int:
        """INSERT с возвратом rowid атомарно под общим lock'ом.

        Отдельная пара execute + SELECT last_insert_rowid() между двумя
        захватами lock'а могла бы вернуть чужой id при конкурентной вставке.
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return int(cur.lastrowid)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def retire_legacy_json(path: Path) -> None:
    """Переименовать легаси-JSON после успешного импорта в SQLite.

    ``users.json`` → ``users.json.migrated``: файл остаётся на диске как бэкап,
    но повторная миграция при следующем старте не срабатывает.
    """
    try:
        path.rename(path.with_suffix(path.suffix + ".migrated"))
        logger.info("db.legacy_retired path=%s", path)
    except OSError as exc:
        logger.warning("db.legacy_retire_failed path=%s error=%s", path, exc)
