"""CLI для аналитики из bot.log* + bot.db (summary_cache, users) + tags_catalog.json.

Запуск (внутри контейнера):

    docker compose exec bot python scripts/analytics.py --days 30

Или с хоста (передав путь к папке логов):

    python3 scripts/analytics.py --logs-dir data/logs --days 30

Выводит markdown-отчёт в stdout. Можно перенаправить в файл:

    docker compose exec bot python scripts/analytics.py --days 7 > report.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

# Чтобы запустить как `python scripts/analytics.py` извне корня проекта.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.log_analytics import aggregate, format_markdown, iter_events  # noqa: E402


def _resolve_logs_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    # Дефолт зависит от того, где мы запускаемся:
    #  - внутри контейнера: /data/logs
    #  - с хоста: data/logs от текущей директории
    if Path("/data/logs").is_dir():
        return Path("/data/logs")
    return Path("data/logs")


def _resolve_data_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    if Path("/data").is_dir():
        return Path("/data")
    return Path("data")


def _resolve_db_path(data_dir: Path, arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    env = os.getenv("DATABASE_PATH")
    if env:
        return Path(env).expanduser()
    return data_dir / "bot.db"


def _open_db_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Открываем bot.db строго read-only: скрипт не должен ни писать,
    ни блокировать живую базу бота (WAL допускает параллельные чтения)."""
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"Не открылась база {db_path}: {exc}", file=sys.stderr)
        return None


def _try_load_summary_cache(conn: sqlite3.Connection | None):
    """Читаем таблицу summary_cache и собираем стаб под format_markdown
    (ему нужен ._entries со свойством .channel_name у записей)."""
    if conn is None:
        return None

    class _CacheStub:
        _entries: dict

    class _EntryStub:
        def __init__(self, channel_name):
            self.channel_name = channel_name

    try:
        rows = conn.execute("SELECT video_id, payload FROM summary_cache").fetchall()
    except sqlite3.Error:
        return None

    entries: dict[str, _EntryStub] = {}
    for vid, payload in rows:
        try:
            body = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(body, dict):
            entries[str(vid)] = _EntryStub(str(body.get("channel_name") or ""))

    stub = _CacheStub()
    stub._entries = entries
    return stub


def _try_load_tags_catalog(data_dir: Path):
    path = data_dir / "tags_catalog.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    class _CatStub:
        def __init__(self, data):
            self._data = data
        def all_tags(self, category):
            return list(self._data.get(category, []))

    return _CatStub(raw or {})


def _load_users(conn: sqlite3.Connection | None) -> dict[str, str]:
    """Читаем таблицу users чтобы резолвить chat_id → имя."""
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT user_id, name FROM users").fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, str] = {}
    for user_id, name in rows:
        uid = str(user_id or "").strip()
        name = str(name or "").strip()
        if uid and name:
            out[uid] = name
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Анализ логов youtube-summary-bot (последние N дней)."
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Окно в днях, считая от now (по умолчанию 30).",
    )
    parser.add_argument(
        "--since", default=None,
        help="ISO-дата нижней границы (YYYY-MM-DD). Перекрывает --days.",
    )
    parser.add_argument(
        "--logs-dir", default=None,
        help="Папка с bot.log*. По умолчанию /data/logs (в контейнере) или ./data/logs.",
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="Папка с bot.db и tags_catalog.json. По умолчанию /data или ./data.",
    )
    parser.add_argument(
        "--db", default=None,
        help="Путь к bot.db. По умолчанию $DATABASE_PATH или <data-dir>/bot.db.",
    )
    args = parser.parse_args()

    logs_dir = _resolve_logs_dir(args.logs_dir)
    data_dir = _resolve_data_dir(args.data_dir)

    if args.since:
        try:
            since_dt = dt.datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(f"--since должен быть YYYY-MM-DD, получили: {args.since!r}", file=sys.stderr)
            return 2
    else:
        since_dt = dt.datetime.now() - dt.timedelta(days=args.days)

    if not logs_dir.is_dir():
        print(f"Логов нет в {logs_dir}. Укажи --logs-dir.", file=sys.stderr)
        return 1

    print(f"# Источник: {logs_dir}, окно от {since_dt.date()}", file=sys.stderr)

    stats = aggregate(iter_events(logs_dir, since=since_dt))

    db_path = _resolve_db_path(data_dir, args.db)
    conn = _open_db_readonly(db_path)
    if conn is None:
        print(f"# Базы нет в {db_path} — отчёт без имён и top-channels", file=sys.stderr)
    try:
        users = _load_users(conn)
        summary_cache = _try_load_summary_cache(conn)
    finally:
        if conn is not None:
            conn.close()
    name_resolver = lambda chat: users.get(chat)
    tags_catalog = _try_load_tags_catalog(data_dir)

    report = format_markdown(
        stats,
        name_resolver=name_resolver,
        summary_cache=summary_cache,
        tags_catalog=tags_catalog,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
