# User Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append-only события по внешним пользователям (первый /start с источником, отказы по квоте, активации/продления подписки) + воронка «старт → первая генерация → paywall → подписка» в owner-команде /stats.

**Architecture:** Новый модуль `app/analytics_events.py` (`AnalyticsEvents` поверх существующего `Database`, таблица `analytics_events`); три точки инструментирования в существующих хендлерах (start / _send_quota_denied / successful_payment); блок воронки приклеивается к выводу /stats. Первая генерация берётся из уже существующей `usage_events` — отдельного события не нужно.

**Tech Stack:** Python 3.11, sqlite3 (существующий app/db.py), pytest.

## Global Constraints

- События пишутся ТОЛЬКО для внешних пользователей (не allowlist): в start-хендлере — под существующей проверкой `not _is_allowed(...)`; `_send_quota_denied` и `successful_payment` и так достижимы только внешними.
- Запись событий никогда не ломает пользовательский путь: каждая точка инструментирования обёрнута try/except с logger.warning.
- `first_start` — одно событие на пользователя за всю жизнь (идемпотентная запись).
- Все события append-only; TTL нет.
- Тексты русские; suite сейчас 82/82, после плана 86/86; вывод pytest чистый.
- Коммит английский, в конце `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: AnalyticsEvents + инструментирование + воронка в /stats

**Files:**
- Create: `app/analytics_events.py`
- Modify: `app/db.py` (схема), `app/services_container.py` (поле Services), `app/main.py` (wiring), `app/bot_handlers.py` (start, successful_payment, /stats), `app/delivery.py` (`_send_quota_denied`), `README.md` (лог/аналитика — 3 строки)
- Test: `tests/test_analytics_events.py`

**Interfaces:**
- Consumes: `Database` (`execute/query_one`), `BillingStore.record_usage` (для фикстур теста воронки), существующие хендлеры.
- Produces:
  - `AnalyticsEvents(db: Database)`: `record(user_id: int, event: str, detail: str = "", now: float | None = None) -> None`; `record_first_start(user_id: int, source: str, now: float | None = None) -> bool` (True если записали, False если у пользователя уже есть first_start); `funnel(days: int, now: float | None = None) -> dict` с ключами `first_starts`, `first_generations`, `quota_denied_users`, `subs_activated`, `sub_renewals`.
  - События: `first_start` (detail: `deep_link`|`organic`), `quota_denied` (detail: deny_reason), `sub_activated` / `sub_renewed` (detail: charge_id).
  - `Services.analytics: "AnalyticsEvents | None" = None`.

- [ ] **Step 1: Схема в db.py**

В `_SCHEMA` перед закрывающей `"""`:

```sql
CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analytics_user_event ON analytics_events(user_id, event);
CREATE INDEX IF NOT EXISTS idx_analytics_event_time ON analytics_events(event, created_at);
```

- [ ] **Step 2: Failing tests**

`tests/test_analytics_events.py`:

```python
from app.analytics_events import AnalyticsEvents
from app.billing import BillingStore
from app.db import Database


NOW = 1_000_000.0
DAY = 86_400.0


def test_first_start_is_idempotent(tmp_path):
    a = AnalyticsEvents(Database(tmp_path / "bot.db"))
    assert a.record_first_start(1, "organic", now=NOW) is True
    assert a.record_first_start(1, "deep_link", now=NOW + 10) is False
    f = a.funnel(days=30, now=NOW + 20)
    assert f["first_starts"] == 1


def test_funnel_counts(tmp_path):
    db = Database(tmp_path / "bot.db")
    a = AnalyticsEvents(db)
    billing = BillingStore(db)

    # Три новых пользователя пришли в окне.
    for uid, src in ((1, "organic"), (2, "deep_link"), (3, "organic")):
        a.record_first_start(uid, src, now=NOW)
    # Двое дошли до первой генерации (usage_events)...
    billing.record_usage(1, "vid1", 1, "starter", now=NOW + 100)
    billing.record_usage(2, "vid2", 1, "starter", now=NOW + 200)
    # ...у первого это уже вторая генерация — первой она быть не перестаёт.
    billing.record_usage(1, "vid3", 1, "starter", now=NOW + 300)
    # Один упёрся в лимит (дважды — считаем уникальных).
    a.record(1, "quota_denied", "weekly_exhausted", now=NOW + 400)
    a.record(1, "quota_denied", "weekly_exhausted", now=NOW + 500)
    # И подписался; потом одно продление.
    a.record(1, "sub_activated", "ch_1", now=NOW + 600)
    a.record(1, "sub_renewed", "ch_2", now=NOW + 700)

    f = a.funnel(days=30, now=NOW + 1000)
    assert f == {
        "first_starts": 3,
        "first_generations": 2,
        "quota_denied_users": 1,
        "subs_activated": 1,
        "sub_renewals": 1,
    }


def test_funnel_window_excludes_old(tmp_path):
    db = Database(tmp_path / "bot.db")
    a = AnalyticsEvents(db)
    billing = BillingStore(db)
    a.record_first_start(1, "organic", now=NOW - 40 * DAY)     # старый юзер
    billing.record_usage(1, "old", 1, "starter", now=NOW - 40 * DAY)
    billing.record_usage(1, "new", 1, "free", now=NOW)          # НЕ первая генерация
    f = a.funnel(days=30, now=NOW + 1)
    assert f["first_starts"] == 0
    assert f["first_generations"] == 0


def test_record_never_raises_on_bad_db(tmp_path):
    db = Database(tmp_path / "bot.db")
    a = AnalyticsEvents(db)
    db.close()
    # После закрытия БД запись не должна кидать — только warning в лог.
    a.record(1, "quota_denied", "weekly_exhausted", now=NOW)
    assert a.record_first_start(2, "organic", now=NOW) is False
```

Run: `./.venv/bin/pytest tests/test_analytics_events.py -q` — Expected: FAIL (`No module named 'app.analytics_events'`).

- [ ] **Step 3: app/analytics_events.py**

```python
"""Append-only аналитика по внешним пользователям.

События (все — только для не-allowlist пользователей):
- first_start      — первый /start (detail: deep_link | organic), одно на юзера;
- quota_denied     — упёрся в лимит (detail: deny_reason);
- sub_activated    — первая оплата подписки (detail: charge_id);
- sub_renewed      — автопродление (detail: charge_id).

«Первая генерация» отдельным событием не пишется — выводится из
usage_events (MIN(created_at) по пользователю). Запись событий никогда
не должна ломать пользовательский путь: все методы глотают исключения
с warning'ом.
"""
from __future__ import annotations

import logging
import time

from app.db import Database


logger = logging.getLogger(__name__)


class AnalyticsEvents:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, user_id: int, event: str, detail: str = "", now: float | None = None) -> None:
        try:
            self._db.execute(
                "INSERT INTO analytics_events(user_id, event, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, event, detail, now if now is not None else time.time()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics.record_failed event=%s error=%s", event, exc)

    def record_first_start(self, user_id: int, source: str, now: float | None = None) -> bool:
        """Записать первый /start пользователя. Идемпотентно: повторные
        вызовы для того же user_id ничего не пишут и возвращают False."""
        try:
            row = self._db.query_one(
                "SELECT 1 FROM analytics_events WHERE user_id = ? AND event = 'first_start'",
                (user_id,),
            )
            if row is not None:
                return False
            self._db.execute(
                "INSERT INTO analytics_events(user_id, event, detail, created_at) "
                "VALUES (?, 'first_start', ?, ?)",
                (user_id, source, now if now is not None else time.time()),
            )
            logger.info("analytics.first_start user_id=%s source=%s", user_id, source)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics.first_start_failed error=%s", exc)
            return False

    def funnel(self, days: int, now: float | None = None) -> dict:
        """Воронка за последние ``days`` дней (по времени события).

        first_generations — пользователи, чья ПЕРВАЯ строка в usage_events
        попала в окно (старые пользователи с новой активностью не считаются).
        """
        now = now if now is not None else time.time()
        cutoff = now - days * 86400
        try:
            def _count(sql: str) -> int:
                row = self._db.query_one(sql, (cutoff,))
                return int(row["n"]) if row else 0

            return {
                "first_starts": _count(
                    "SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events "
                    "WHERE event = 'first_start' AND created_at > ?"
                ),
                "first_generations": _count(
                    "SELECT COUNT(*) AS n FROM ("
                    "  SELECT user_id, MIN(created_at) AS first_at FROM usage_events"
                    "  GROUP BY user_id) WHERE first_at > ?"
                ),
                "quota_denied_users": _count(
                    "SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events "
                    "WHERE event = 'quota_denied' AND created_at > ?"
                ),
                "subs_activated": _count(
                    "SELECT COUNT(DISTINCT user_id) AS n FROM analytics_events "
                    "WHERE event = 'sub_activated' AND created_at > ?"
                ),
                "sub_renewals": _count(
                    "SELECT COUNT(*) AS n FROM analytics_events "
                    "WHERE event = 'sub_renewed' AND created_at > ?"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics.funnel_failed error=%s", exc)
            return {
                "first_starts": 0, "first_generations": 0,
                "quota_denied_users": 0, "subs_activated": 0, "sub_renewals": 0,
            }
```

- [ ] **Step 4: Wiring**

1. `app/services_container.py`: в `Services` рядом с `analytics`-соседями (`billing`/`quota`): `analytics: "AnalyticsEvents | None" = None` (+ импорт `from app.analytics_events import AnalyticsEvents`).
2. `app/main.py`: после создания `quota_service`: `analytics_events = AnalyticsEvents(db)` (+ импорт), передать `analytics=analytics_events` в `Services(...)`.

- [ ] **Step 5: Инструментирование**

1. `app/bot_handlers.py`, хендлер `start`: в deep-link ветке (после `logger.info("deep_link.start ...")`, ПЕРЕД `_enqueue_summary_job`) и в онбординг-ветке для внешних (внутри существующей ветки `if _is_allowed... return` — в её else, т.е. перед отправкой онбординга):

```python
        # deep-link ветка:
        if services.analytics is not None and not _is_allowed(message, services):
            user_id = _message_user_id(message)
            if user_id is not None:
                services.analytics.record_first_start(user_id, "deep_link")
```

```python
        # онбординг-ветка (external), перед message.answer(...):
        if services.analytics is not None:
            user_id = _message_user_id(message)
            if user_id is not None:
                services.analytics.record_first_start(user_id, "organic")
```

2. `app/bot_handlers.py`, хендлер `successful_payment`: после `activate_subscription(...)`:

```python
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
```

3. `app/delivery.py`, `_send_quota_denied`: в начале функции:

```python
    user_id = _message_user_id(message)
    if services.analytics is not None and user_id is not None:
        services.analytics.record(user_id, "quota_denied", detail=verdict.deny_reason)
```

(`_message_user_id` уже в delivery.py.)

- [ ] **Step 6: Воронка в /stats**

`app/bot_handlers.py`, хендлер `stats`: рядом с существующим блоком `db_line` (job counts) добавить:

```python
        funnel_line = ""
        if services.analytics is not None:
            f = services.analytics.funnel(30)
            funnel_line = (
                "Воронка внешних пользователей (30 дн):\n"
                f"/start: {f['first_starts']} → первая генерация: {f['first_generations']} → "
                f"упёрлись в лимит: {f['quota_denied_users']} → подписка: {f['subs_activated']} "
                f"(продлений: {f['sub_renewals']})\n\n"
            )
```
и приклеить `funnel_line` после `db_line` в итоговом тексте.

- [ ] **Step 7: README**

В раздел «Монетизация (PUBLIC_MODE)» — абзац: события аналитики (`analytics_events`: first_start/quota_denied/sub_activated/sub_renewed) копятся бессрочно; воронка за 30 дней — в `/stats`; лог-событие `analytics.first_start`.

- [ ] **Step 8: Прогнать всё**

```bash
./.venv/bin/pytest tests/ -q          # 86 passed
python3 -m compileall app/ -q
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "User analytics: append-only events (first_start, quota_denied, sub_*) and funnel in /stats"
```
