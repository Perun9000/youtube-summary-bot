"""CLI для аналитики из bot.log* + summary_cache.json + tags_catalog.json.

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


def _try_load_summary_cache(data_dir: Path):
    """Загружаем summary_cache.json «руками», без TagsCatalog/lock."""
    path = data_dir / "summary_cache.json"
    if not path.exists():
        return None

    class _CacheStub:
        _entries: dict

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    # Маппим минимально: нам нужен .channel_name для top-channels.
    class _EntryStub:
        def __init__(self, body):
            self.channel_name = body.get("channel_name", "")

    stub = _CacheStub()
    stub._entries = {vid: _EntryStub(body) for vid, body in raw.items() if isinstance(body, dict)}
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


def _load_users(data_dir: Path) -> dict[str, str]:
    """Загружаем data/users.json чтобы резолвить chat_id → имя."""
    path = data_dir / "users.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for user in (raw.get("users") or []):
            if not isinstance(user, dict):
                continue
            uid = str(user.get("user_id") or "").strip()
            name = str(user.get("name") or "").strip()
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
        help="Папка с *.json (summary_cache, tags_catalog, users). По умолчанию /data или ./data.",
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

    users = _load_users(data_dir)
    name_resolver = lambda chat: users.get(chat)
    summary_cache = _try_load_summary_cache(data_dir)
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
