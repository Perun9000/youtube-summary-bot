# Stars Monetization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Открыть бота внешним пользователям с квотами (3 стартовых / 1 в неделю бесплатно / 30 в месяц по подписке) и подпиской за 149 Telegram Stars в месяц с автопродлением.

**Architecture:** Новый модуль `app/billing.py`: `BillingStore` (таблицы `subscriptions` + `usage_events` в существующем `data/bot.db`) и `QuotaService` (чистая логика вердиктов). Гейт — в `_enqueue_summary_job` ДО постановки в очередь; списание — в pipeline только после успешной генерации (кэш-хиты бесплатны); тяжёлые ролики (без субтитров, ≥1 ч) — вес 2 с предупреждением. Платежи — нативные Stars-подписки Bot API (`create_invoice_link` с `subscription_period`), обязательный `/paysupport`, owner-команда `/refund`. Всё за флагом `PUBLIC_MODE` (default false — текущее закрытое поведение не меняется, пока флаг не включён).

**Tech Stack:** Python 3.11, aiogram ≥3.14 (в venv 3.22 — Stars-подписки поддержаны), sqlite3 (существующий `app/db.py`), pytest.

## Global Constraints

- Значения по умолчанию (env-переопределяемые): `SUBSCRIPTION_PRICE_STARS=149`, `QUOTA_STARTER=3`, `QUOTA_FREE_WEEKLY=1`, `QUOTA_SUB_MONTHLY=30`, `HEAVY_DURATION_SEC=3600`, `PUBLIC_MODE=false`.
- Пользователи из allowlist (`users`-таблица) обходят квоты и подписку полностью; их расход НЕ записывается. Owner-команды и мониторинг — без изменений.
- При `PUBLIC_MODE=false` поведение бота идентично текущему (все 5 отказов «Этот бот закрыт…» работают как раньше).
- Окна квот скользящие: неделя = 7×86400 сек, месяц = 30×86400 сек. Стартовые — lifetime (первые 3 события).
- Списание — только после успешной генерации; кэш-хиты и упавшие job'ы не списывают.
- Валюта инвойсов строго `XTR`, `subscription_period=2592000` (30 дней — единственное значение, которое принимает Bot API).
- Все пользовательские тексты — русские, стиль существующих сообщений бота; суммы — «149 ⭐».
- Публичные API существующих сторов не меняются. TDD; suite сейчас 59/59 зелёный; вывод pytest чистый.
- Коммиты — английские, в конце строка `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: BillingStore + QuotaService (ядро, чистая логика)

**Files:**
- Create: `app/billing.py`
- Modify: `app/db.py` (схема: 2 таблицы + индекс)
- Test: `tests/test_billing.py`

**Interfaces:**
- Produces:
  - `BillingStore(db: Database)`: `activate_subscription(user_id: int, until_unix: float, charge_id: str) -> None`; `subscription_until(user_id: int) -> float` (0.0 если нет); `is_subscriber(user_id: int, now: float | None = None) -> bool`; `last_charge_id(user_id: int) -> str`; `record_usage(user_id: int, video_id: str, weight: int, kind: str, now: float | None = None) -> None`; `usage_since(user_id: int, since: float, kind: str | None = None) -> int` (сумма weight); `total_usage(user_id: int) -> int`; `active_subscribers_count(now: float | None = None) -> int`.
  - `QuotaVerdict` (frozen dataclass): `allowed: bool`, `kind: str` ('starter'|'free'|'sub'|''), `remaining: int`, `is_subscriber: bool`, `deny_reason: str` (''|'weekly_exhausted'|'monthly_exhausted').
  - `QuotaService(store: BillingStore, *, starter: int, weekly: int, monthly: int)`: `check(user_id: int, weight: int = 1, now: float | None = None) -> QuotaVerdict`; `charge(user_id: int, video_id: str, weight: int, now: float | None = None) -> None` (сам определяет kind тем же правилом и пишет usage).
  - Константы: `WEEK_SEC = 7 * 86400`, `MONTH_SEC = 30 * 86400`.

- [ ] **Step 1: Схема в db.py**

В `_SCHEMA` в `app/db.py` добавить перед закрывающей `"""`:

```sql
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
```

(`CREATE IF NOT EXISTS` создаст таблицы на существующей базе — ALTER не нужен.)

- [ ] **Step 2: Failing tests**

`tests/test_billing.py`:

```python
from app.billing import BillingStore, QuotaService, WEEK_SEC, MONTH_SEC
from app.db import Database


NOW = 1_000_000.0


def make(tmp_path, starter=3, weekly=1, monthly=30):
    store = BillingStore(Database(tmp_path / "bot.db"))
    quota = QuotaService(store, starter=starter, weekly=weekly, monthly=monthly)
    return store, quota


def test_starter_pack_then_weekly(tmp_path):
    store, quota = make(tmp_path)
    # Первые 3 генерации — стартовые.
    for i in range(3):
        v = quota.check(1, now=NOW)
        assert v.allowed and v.kind == "starter"
        assert v.remaining == 3 - i
        quota.charge(1, f"vid{i}", 1, now=NOW)
    # Стартовые кончились, недельная (1/нед) свободна.
    v = quota.check(1, now=NOW)
    assert v.allowed and v.kind == "free" and v.remaining == 1
    quota.charge(1, "vid3", 1, now=NOW)
    # Неделя занята.
    v = quota.check(1, now=NOW + 1)
    assert not v.allowed and v.deny_reason == "weekly_exhausted"
    # Через 7 дней — снова можно.
    v = quota.check(1, now=NOW + WEEK_SEC + 1)
    assert v.allowed and v.kind == "free"


def test_subscriber_monthly_limit_and_expiry(tmp_path):
    store, quota = make(tmp_path, monthly=5)
    store.activate_subscription(1, until_unix=NOW + MONTH_SEC, charge_id="ch_1")
    assert store.is_subscriber(1, now=NOW)
    assert store.last_charge_id(1) == "ch_1"
    for i in range(5):
        v = quota.check(1, now=NOW)
        assert v.allowed and v.kind == "sub" and v.remaining == 5 - i
        quota.charge(1, f"v{i}", 1, now=NOW)
    v = quota.check(1, now=NOW)
    assert not v.allowed and v.deny_reason == "monthly_exhausted" and v.is_subscriber
    # Окно скользящее: через 30 дней расход «испарился», но подписка истекла →
    # пользователь снова free (стартовые уже сожжены зачётом total_usage).
    v = quota.check(1, now=NOW + MONTH_SEC + 1)
    assert not v.is_subscriber


def test_heavy_weight_respects_remaining(tmp_path):
    store, quota = make(tmp_path, starter=1)
    quota.charge(1, "v0", 1, now=NOW)          # стартовый сожжён
    v = quota.check(1, weight=2, now=NOW)       # weekly=1 < weight=2
    assert not v.allowed and v.deny_reason == "weekly_exhausted"
    store.activate_subscription(1, until_unix=NOW + MONTH_SEC, charge_id="c")
    v = quota.check(1, weight=2, now=NOW)
    assert v.allowed and v.kind == "sub"
    quota.charge(1, "v1", 2, now=NOW)
    assert store.usage_since(1, NOW - 1) == 3   # 1 + 2 (сумма weight)


def test_renewal_extends_subscription(tmp_path):
    store, _ = make(tmp_path)
    store.activate_subscription(1, until_unix=NOW + MONTH_SEC, charge_id="c1")
    store.activate_subscription(1, until_unix=NOW + 2 * MONTH_SEC, charge_id="c2")
    assert store.subscription_until(1) == NOW + 2 * MONTH_SEC
    assert store.last_charge_id(1) == "c2"
    assert store.active_subscribers_count(now=NOW) == 1
    assert store.active_subscribers_count(now=NOW + 3 * MONTH_SEC) == 0
```

Run: `./.venv/bin/pytest tests/test_billing.py -q` — Expected: FAIL (`No module named 'app.billing'`).

- [ ] **Step 3: app/billing.py**

```python
"""Биллинг: подписки Stars и квоты генераций.

Модель доступа (за флагом PUBLIC_MODE):
- allowlist (таблица users) — безлимит, этот модуль их не видит вообще;
- внешний без подписки — первые QUOTA_STARTER генераций (lifetime),
  дальше QUOTA_FREE_WEEKLY за скользящие 7 дней;
- подписчик (149 Stars/мес, автопродление) — QUOTA_SUB_MONTHLY за
  скользящие 30 дней.
Списание (charge) вызывается ТОЛЬКО после успешной генерации; кэш-хиты
и упавшие job'ы бесплатны. Тяжёлые ролики (Groq-транскрипция, ≥1 ч)
списываются с weight=2 — их себестоимость на порядок выше.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.db import Database


logger = logging.getLogger(__name__)

WEEK_SEC = 7 * 86400
MONTH_SEC = 30 * 86400


@dataclass(frozen=True)
class QuotaVerdict:
    allowed: bool
    kind: str            # 'starter' | 'free' | 'sub' | ''
    remaining: int       # сколько ЕЩЁ можно списать в текущем окне (включая запрошенный weight)
    is_subscriber: bool
    deny_reason: str = ""  # '' | 'weekly_exhausted' | 'monthly_exhausted'


class BillingStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ── подписки ──────────────────────────────────────────────────────

    def activate_subscription(self, user_id: int, until_unix: float, charge_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO subscriptions(user_id, until_unix, last_charge_id, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, until_unix, charge_id, time.time()),
        )
        logger.info(
            "billing.subscription.activated user_id=%s until_unix=%.0f charge_id=%s",
            user_id, until_unix, charge_id,
        )

    def subscription_until(self, user_id: int) -> float:
        row = self._db.query_one(
            "SELECT until_unix FROM subscriptions WHERE user_id = ?", (user_id,)
        )
        return float(row["until_unix"]) if row else 0.0

    def is_subscriber(self, user_id: int, now: float | None = None) -> bool:
        return self.subscription_until(user_id) > (now if now is not None else time.time())

    def last_charge_id(self, user_id: int) -> str:
        row = self._db.query_one(
            "SELECT last_charge_id FROM subscriptions WHERE user_id = ?", (user_id,)
        )
        return str(row["last_charge_id"]) if row else ""

    def active_subscribers_count(self, now: float | None = None) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE until_unix > ?",
            ((now if now is not None else time.time()),),
        )
        return int(row["n"]) if row else 0

    # ── расход ────────────────────────────────────────────────────────

    def record_usage(
        self, user_id: int, video_id: str, weight: int, kind: str, now: float | None = None
    ) -> None:
        self._db.execute(
            "INSERT INTO usage_events(user_id, video_id, weight, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, video_id, weight, kind, now if now is not None else time.time()),
        )

    def usage_since(self, user_id: int, since: float, kind: str | None = None) -> int:
        if kind is None:
            row = self._db.query_one(
                "SELECT COALESCE(SUM(weight), 0) AS s FROM usage_events "
                "WHERE user_id = ? AND created_at > ?",
                (user_id, since),
            )
        else:
            row = self._db.query_one(
                "SELECT COALESCE(SUM(weight), 0) AS s FROM usage_events "
                "WHERE user_id = ? AND created_at > ? AND kind = ?",
                (user_id, since, kind),
            )
        return int(row["s"]) if row else 0

    def total_usage(self, user_id: int) -> int:
        row = self._db.query_one(
            "SELECT COALESCE(SUM(weight), 0) AS s FROM usage_events WHERE user_id = ?",
            (user_id,),
        )
        return int(row["s"]) if row else 0


class QuotaService:
    def __init__(self, store: BillingStore, *, starter: int, weekly: int, monthly: int) -> None:
        self._store = store
        self._starter = starter
        self._weekly = weekly
        self._monthly = monthly

    def check(self, user_id: int, weight: int = 1, now: float | None = None) -> QuotaVerdict:
        now = now if now is not None else time.time()
        if self._store.is_subscriber(user_id, now=now):
            used = self._store.usage_since(user_id, now - MONTH_SEC)
            remaining = max(0, self._monthly - used)
            if remaining >= weight:
                return QuotaVerdict(True, "sub", remaining, True)
            return QuotaVerdict(False, "", remaining, True, "monthly_exhausted")

        total = self._store.total_usage(user_id)
        if total + weight <= self._starter:
            return QuotaVerdict(True, "starter", self._starter - total, False)

        # Недельное окно считает только post-starter события (kind='free'):
        # сожжённый в день 1 стартовый пакет не должен блокировать
        # недельную бесплатную генерацию.
        used_week = self._store.usage_since(user_id, now - WEEK_SEC, kind="free")
        remaining = max(0, self._weekly - used_week)
        if remaining >= weight:
            return QuotaVerdict(True, "free", remaining, False)
        return QuotaVerdict(False, "", remaining, False, "weekly_exhausted")

    def charge(self, user_id: int, video_id: str, weight: int, now: float | None = None) -> None:
        """Записать расход. Kind определяется тем же правилом, что и check().

        Вызывается после успешной генерации; воркер последовательный, так что
        гонка check→charge исключена по построению.
        """
        now = now if now is not None else time.time()
        verdict = self.check(user_id, weight=weight, now=now)
        kind = verdict.kind or ("sub" if verdict.is_subscriber else "free")
        self._store.record_usage(user_id, video_id, weight, kind, now=now)
        logger.info(
            "billing.usage.charged user_id=%s video_id=%s weight=%s kind=%s",
            user_id, video_id, weight, kind,
        )
```

- [ ] **Step 4: Прогнать тесты**

Run: `./.venv/bin/pytest tests/test_billing.py -q` — Expected: PASS (4). Затем весь suite: `./.venv/bin/pytest tests/ -q` — 63 passed.

- [ ] **Step 5: Commit**

```bash
git add app/billing.py app/db.py tests/test_billing.py
git commit -m "Billing core: subscriptions + usage quotas (starter/weekly/monthly, weighted)"
```

---

### Task 2: Конфиг, wiring и публичный режим доступа

**Files:**
- Modify: `app/config.py`, `app/main.py`, `app/services_container.py`, `app/bot_handlers.py`
- Test: `tests/test_config.py` (расширить)

**Interfaces:**
- Consumes: `BillingStore`, `QuotaService` (Task 1).
- Produces:
  - `Settings`: новые поля `public_mode: bool`, `subscription_price_stars: int`, `quota_starter: int`, `quota_free_weekly: int`, `quota_sub_monthly: int`, `heavy_duration_sec: int`.
  - `Services`: поля `billing: "BillingStore | None" = None`, `quota: "QuotaService | None" = None`.
  - `_has_access(message, services) -> bool` в bot_handlers: allowlist ИЛИ public_mode.
  - Команда `/limits` — статус лимитов/подписки для любого пользователя с доступом.

- [ ] **Step 1: Failing test на конфиг**

Дописать в `tests/test_config.py`:

```python
def test_monetization_defaults(base_env):
    settings = load_settings()
    assert settings.public_mode is False
    assert settings.subscription_price_stars == 149
    assert settings.quota_starter == 3
    assert settings.quota_free_weekly == 1
    assert settings.quota_sub_monthly == 30
    assert settings.heavy_duration_sec == 3600


def test_public_mode_flag(base_env, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "true")
    assert load_settings().public_mode is True
```

Run: `./.venv/bin/pytest tests/test_config.py -q` — FAIL (нет полей).

- [ ] **Step 2: config.py**

В `Settings` добавить поля (рядом с `premiere_delay_hours`):

```python
    public_mode: bool
    subscription_price_stars: int
    quota_starter: int
    quota_free_weekly: int
    quota_sub_monthly: int
    heavy_duration_sec: int
```

В `load_settings()` в hoisted-блоке (рядом с `premiere_delay_hours = ...`):

```python
    # Монетизация: PUBLIC_MODE открывает бота внешним пользователям с квотами
    # и подпиской за Stars. false — прежнее закрытое поведение (allowlist).
    public_mode = os.getenv("PUBLIC_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
    subscription_price_stars = env.int("SUBSCRIPTION_PRICE_STARS", "149")
    quota_starter = env.int("QUOTA_STARTER", "3")
    quota_free_weekly = env.int("QUOTA_FREE_WEEKLY", "1")
    quota_sub_monthly = env.int("QUOTA_SUB_MONTHLY", "30")
    heavy_duration_sec = env.int("HEAVY_DURATION_SEC", "3600")
```

и передать все шесть в `Settings(...)`.

- [ ] **Step 3: Services + main.py**

`app/services_container.py`: в `Services` добавить (рядом с `morning_digest`):

```python
    billing: "BillingStore | None" = None
    quota: "QuotaService | None" = None
```

(+ импорт `from app.billing import BillingStore, QuotaService` — под TYPE_CHECKING, если сложится цикл; цикла быть не должно: billing зависит только от db.)

`app/main.py`: после создания `db`:

```python
    billing_store = BillingStore(db)
    quota_service = QuotaService(
        billing_store,
        starter=settings.quota_starter,
        weekly=settings.quota_free_weekly,
        monthly=settings.quota_sub_monthly,
    )
    logger.info(
        "billing.boot public_mode=%s price_stars=%s quotas=%s/%s/%s",
        settings.public_mode, settings.subscription_price_stars,
        settings.quota_starter, settings.quota_free_weekly, settings.quota_sub_monthly,
    )
```

и `billing=billing_store, quota=quota_service` в конструктор `Services`.

- [ ] **Step 4: Публичный доступ в bot_handlers.py**

1. Новый helper рядом с `_is_allowed`:

```python
def _has_access(message: Message, services: Services) -> bool:
    """Allowlist — всегда да; внешние — только при PUBLIC_MODE."""
    return _is_allowed(message, services) or services.settings.public_mode
```

2. `grep -n "Этот бот закрыт" app/bot_handlers.py` — 5 мест. В хендлерах `start`, `help_command`, `last_command`, `text_message` заменить условие `if not _is_allowed(message, services):` на `if not _has_access(message, services):` (текст отказа оставить прежним). В `_answer_owner_only` — НЕ трогать (owner-гейт).
3. В `start` и `help_command`: для пользователей вне allowlist (`not _is_allowed(...)` при `public_mode`) добавить к тексту блок о лимитах:

```python
        if not _is_allowed(message, services):
            text += (
                "\n\nБесплатно: "
                f"{services.settings.quota_starter} саммари на старте, "
                f"дальше {services.settings.quota_free_weekly} в неделю. "
                f"Подписка {services.settings.subscription_price_stars} ⭐/мес — "
                f"{services.settings.quota_sub_monthly} саммари в месяц: /subscribe. "
                "Остаток лимитов: /limits."
            )
```

(интегрировать в фактическую структуру текстов start/help — прочитать хендлеры перед правкой).
4. Команда `/limits`:

```python
    @router.message(Command("limits"))
    async def limits(message: Message) -> None:
        if not _has_access(message, services):
            await message.answer("Этот бот закрыт для личного использования.")
            return
        user_id = _message_user_id(message)
        if user_id is None or services.quota is None or services.billing is None:
            await message.answer("Лимиты не настроены.")
            return
        if _is_allowed(message, services):
            await message.answer("У тебя безлимитный доступ 🎉")
            return
        s = services.settings
        verdict = services.quota.check(user_id)
        if verdict.is_subscriber:
            until = services.billing.subscription_until(user_id)
            until_text = datetime.datetime.fromtimestamp(until).strftime("%d.%m.%Y")
            await message.answer(
                f"Подписка активна до {until_text}.\n"
                f"Осталось в этом месяце: {verdict.remaining} из {s.quota_sub_monthly}."
            )
            return
        if verdict.kind == "starter":
            await message.answer(
                f"Стартовых саммари осталось: {verdict.remaining} из {s.quota_starter}.\n"
                f"Дальше — {s.quota_free_weekly} в неделю бесплатно или подписка: /subscribe."
            )
            return
        if verdict.allowed:
            await message.answer(
                f"Доступно бесплатных на этой неделе: {verdict.remaining} из {s.quota_free_weekly}.\n"
                f"Больше — по подписке {s.subscription_price_stars} ⭐/мес: /subscribe."
            )
        else:
            await message.answer(
                "Бесплатный лимит на эту неделю исчерпан.\n"
                f"Подписка {s.subscription_price_stars} ⭐/мес — "
                f"{s.quota_sub_monthly} саммари: /subscribe."
            )
```

5. `app/main.py`: `BotCommand(command="limits", description="Остаток лимитов")` в `PUBLIC_BOT_COMMANDS`.

- [ ] **Step 5: Прогнать suite + smoke**

`./.venv/bin/pytest tests/ -q` (65 passed) и `python3 -m compileall app/ -q`. Поведение при выключенном PUBLIC_MODE не изменилось (отказы на месте — проверить глазами diff).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "Public mode config, billing wiring, /limits command"
```

---

### Task 3: Квота-гейт в очереди, вес тяжёлых роликов, списание после успеха

**Files:**
- Modify: `app/services_container.py` (SummaryJob), `app/queue_service.py` (`_enqueue_summary_job`), `app/pipeline.py`, `app/delivery.py` (кнопка подписки)
- Test: ручная трассировка в отчёте (интеграционные точки; юнит-логика покрыта Task 1)

**Interfaces:**
- Consumes: `QuotaService.check/charge`, `QuotaVerdict`, `Settings.heavy_duration_sec`, `_has_access`.
- Produces:
  - `SummaryJob.quota_user_id: int | None = None` (None = безлимит: allowlist/owner/scheduled), `SummaryJob.usage_weight: int = 1`.
  - `async def _send_quota_denied(message, services, verdict) -> None` в `app/delivery.py` — отказ с кнопкой «Оформить подписку 149 ⭐» (inline-кнопка `callback_data="subscribe"` — хендлер в Task 4).

- [ ] **Step 1: SummaryJob поля**

`app/services_container.py`, в `SummaryJob` после `deferred_until`:

```python
    # Квоты внешних пользователей (PUBLIC_MODE). None — безлимит (allowlist,
    # owner, scheduled-мониторинг): ни проверок, ни списаний.
    quota_user_id: int | None = None
    # Вес списания: 1 обычный ролик, 2 — тяжёлый (Groq-транскрипция, ≥1 ч).
    # Выставляется в pipeline, когда выясняется источник транскрипта.
    usage_weight: int = 1
```

- [ ] **Step 2: Гейт в `_enqueue_summary_job` (queue_service.py)**

В начало функции, ДО существующего `try:` с cache fast-path добавить определение платности, а квота-проверку — ПОСЛЕ кэш-проверки (кэш бесплатен), но ДО постановки в очередь. Итоговая структура начала функции:

```python
async def _enqueue_summary_job(message: Message, url: str, services: Services) -> None:
    # Внешний пользователь (не allowlist) в PUBLIC_MODE проходит через квоты.
    from app.bot_handlers import _is_allowed  # local: избегаем цикла
    quota_user_id: int | None = None
    if not _is_allowed(message, services) and message.from_user is not None:
        quota_user_id = message.from_user.id

    try:
        # Cache hit fast-path: ... (существующий код без изменений; кэш бесплатен)
        cached = _lookup_cached_summary(url, services)
        if cached is not None:
            ...
            return

        if quota_user_id is not None and services.quota is not None:
            verdict = services.quota.check(quota_user_id)
            if not verdict.allowed:
                await _send_quota_denied(message, services, verdict)
                return  # сообщение пользователя НЕ удаляем — finally ниже пропустит
        ...
```

ВАЖНО про `finally`-блок функции (он удаляет исходное сообщение пользователя): при отказе по квоте сообщение со ссылкой удалять нельзя. Реализация: завести локальный флаг `enqueued = False`, ставить `True` после `queue.put` и в cache-hit ветке; в `finally` удалять только `if enqueued`.

Прокинуть `quota_user_id=quota_user_id` в конструктор `SummaryJob(...)`.

NB: цикл импортов bot_handlers↔queue_service уже разорван реэкспортом — `_is_allowed` импортировать локально внутри функции (bot_handlers сам импортирует queue_service на уровне модуля).

- [ ] **Step 3: `_send_quota_denied` в delivery.py**

```python
async def _send_quota_denied(message: Message, services: Services, verdict) -> None:
    """Отказ по квоте + кнопка оформления подписки.

    callback 'subscribe' обрабатывается в bot_handlers (шлёт Stars-инвойс) —
    кнопка работает и из этого сообщения, и из /subscribe.
    """
    s = services.settings
    if verdict.deny_reason == "monthly_exhausted":
        text = (
            f"Лимит подписки на месяц исчерпан ({s.quota_sub_monthly} саммари). "
            "Новые генерации станут доступны по мере «оттаивания» окна 30 дней — /limits."
        )
        await message.answer(text)
        return
    text = (
        "Бесплатный лимит на эту неделю исчерпан.\n\n"
        f"Подписка — {s.subscription_price_stars} ⭐/мес: "
        f"{s.quota_sub_monthly} саммари в месяц, автопродление, отмена в любой момент.\n"
        "Остаток лимитов: /limits."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"Оформить подписку — {s.subscription_price_stars} ⭐",
            callback_data="subscribe",
        )
    ]])
    await message.answer(text, reply_markup=keyboard)
```

- [ ] **Step 4: Вес тяжёлых + предупреждение (pipeline.py)**

В `_process_youtube_job`, в ветке `except TranscriptUnavailable:` ПЕРЕД `await _enqueue_transcription_job(job, services)` добавить:

```python
                # Тяжёлый ролик для платного пользователя: Groq-транскрипция
                # длинного видео стоит на порядок дороже — списываем 2 единицы.
                if (
                    job.quota_user_id is not None
                    and services.quota is not None
                    and (metadata.duration_sec or 0) >= services.settings.heavy_duration_sec
                ):
                    verdict = services.quota.check(job.quota_user_id, weight=2)
                    if not verdict.allowed:
                        raise RuntimeError(
                            "у ролика нет субтитров, и он длиннее часа — такая "
                            "генерация списывает 2 единицы лимита, а осталось "
                            f"{verdict.remaining}. Подписка: /subscribe"
                        )
                    job.usage_weight = 2
                    await _set_service_status(
                        services, message,
                        "Ролик без субтитров и длиннее часа — спишется 2 генерации.",
                        job=job,
                    )
```

(RuntimeError уйдёт пользователю существующим error-путём «генерация прервана. Причина: …».)

- [ ] **Step 5: Списание после успеха (pipeline.py)**

В `_process_youtube_job`, в успешном пути СРАЗУ ПОСЛЕ блока доставки (if/else manual/scheduled) и ПЕРЕД блоком кэширования:

```python
        # Списываем квоту только после успешной доставки. Кэш-хиты сюда не
        # доходят (fast-path выше), упавшие job'ы — тоже (except-ветка).
        if job.quota_user_id is not None and services.quota is not None:
            try:
                services.quota.charge(job.quota_user_id, video_id, job.usage_weight)
            except Exception:
                logger.exception("billing.charge_failed user_id=%s", job.quota_user_id)
```

- [ ] **Step 6: Проверка + Commit**

`./.venv/bin/pytest tests/ -q` + `python3 -m compileall app/ -q`. В отчёте имплементера — трассировка путей: (a) внешний с квотой → enqueue → успех → charge(1); (b) кэш-хит → без charge; (c) тяжёлый с остатком 1 → отказ до транскрипции; (d) allowlist → quota_user_id=None везде; (e) отказ по квоте → сообщение пользователя не удалено.

```bash
git add -A && git commit -m "Quota gate at enqueue, heavy-video weight, charge on success"
```

---

### Task 4: Stars-платежи: /subscribe, инвойсы, /paysupport, /refund

**Files:**
- Modify: `app/bot_handlers.py`, `app/main.py` (списки команд)
- Test: юнит на хелпер `_subscription_until_from_payment` (tests/test_billing.py) + ручной чеклист

**Interfaces:**
- Consumes: `BillingStore.activate_subscription/last_charge_id`, `Settings.subscription_price_stars`.
- Produces: команды `/subscribe`, `/paysupport`; callback `subscribe`; хендлеры `pre_checkout_query` и `successful_payment`; owner-команда `/refund <user_id> [charge_id]`.

- [ ] **Step 1: Failing test на хелпер конверсии срока**

В `tests/test_billing.py`:

```python
import datetime

from app.bot_handlers import _subscription_until_from_payment


class _FakePayment:
    def __init__(self, expiration):
        self.subscription_expiration_date = expiration


def test_subscription_until_from_payment_datetime():
    dt = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.timezone.utc)
    assert _subscription_until_from_payment(_FakePayment(dt), now=0.0) == dt.timestamp()


def test_subscription_until_from_payment_fallback_30d():
    assert _subscription_until_from_payment(_FakePayment(None), now=1000.0) == 1000.0 + 30 * 86400
```

Run — FAIL (нет функции).

- [ ] **Step 2: Хендлеры в bot_handlers.py**

Импорты: `from aiogram.types import LabeledPrice, PreCheckoutQuery` (дополнить существующий import-блок aiogram.types), `from app.billing import MONTH_SEC`.

```python
SUBSCRIPTION_PAYLOAD = "monthly_summary_subscription"


def _subscription_until_from_payment(payment, now: float | None = None) -> float:
    """Срок подписки из SuccessfulPayment: Telegram присылает
    subscription_expiration_date (datetime); если его нет — 30 дней от now."""
    now = now if now is not None else time.time()
    expiration = getattr(payment, "subscription_expiration_date", None)
    if expiration is not None:
        return expiration.timestamp()
    return now + MONTH_SEC


async def _send_subscription_invoice(chat_id: int, services: Services) -> None:
    """Инвойс нативной Stars-подписки (валюта XTR, автопродление 30 дней).

    subscription_period поддерживается только в createInvoiceLink, поэтому
    шлём ссылку кнопкой, а не send_invoice.
    """
    s = services.settings
    link = await services.bot.create_invoice_link(
        title="Подписка на саммари",
        description=(
            f"{s.quota_sub_monthly} саммари в месяц. Автопродление каждые 30 дней, "
            "отмена в любой момент в настройках Telegram."
        ),
        payload=SUBSCRIPTION_PAYLOAD,
        currency="XTR",
        prices=[LabeledPrice(label="Подписка на месяц", amount=s.subscription_price_stars)],
        subscription_period=2592000,
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"Оплатить {s.subscription_price_stars} ⭐", url=link
        )
    ]])
    await services.bot.send_message(
        chat_id=chat_id,
        text=(
            f"Подписка: {s.quota_sub_monthly} саммари в месяц за "
            f"{s.subscription_price_stars} ⭐.\n"
            "Вопросы по оплате: /paysupport."
        ),
        reply_markup=keyboard,
    )
```

Внутри `build_router`:

```python
    @router.message(Command("subscribe"))
    async def subscribe(message: Message) -> None:
        if not _has_access(message, services):
            await message.answer("Этот бот закрыт для личного использования.")
            return
        if _is_allowed(message, services):
            await message.answer("У тебя и так безлимитный доступ 🎉")
            return
        await _send_subscription_invoice(message.chat.id, services)

    @router.callback_query(F.data == "subscribe")
    async def subscribe_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message is not None:
            await _send_subscription_invoice(callback.message.chat.id, services)

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
        until_text = datetime.datetime.fromtimestamp(until).strftime("%d.%m.%Y")
        await message.answer(
            f"Подписка активна до {until_text} — {services.settings.quota_sub_monthly} "
            "саммари в месяц. Остаток: /limits. Спасибо! 🔮"
        )

    @router.message(Command("paysupport"))
    async def paysupport(message: Message) -> None:
        # Обязательная команда по ToS Telegram для ботов, принимающих Stars.
        owner = services.settings.owner_user_id
        contact = f'<a href="tg://user?id={owner}">владельцу бота</a>' if owner else "владельцу бота"
        await message.answer(
            "По вопросам оплаты и возвратов напиши " + contact + ". "
            "Возвраты выполняются вручную в течение 1–2 дней.",
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
```

- [ ] **Step 3: Списки команд в main.py**

`PUBLIC_BOT_COMMANDS` += `subscribe` («Подписка»), `paysupport` («Вопросы по оплате»); `OWNER_BOT_COMMANDS` += `refund` («Возврат платежа Stars»).

- [ ] **Step 4: Тесты + Commit**

`./.venv/bin/pytest tests/ -q` (67 passed), compileall.

```bash
git add -A && git commit -m "Stars payments: /subscribe invoice link, successful_payment, /paysupport, owner /refund"
```

---

### Task 5: Документация и верификация

**Files:**
- Modify: `README.md`, `.env.example`

- [ ] **Step 1: .env.example**

Добавить блок после `PREMIERE_SUMMARY_DELAY_HOURS`:

```dotenv
# Монетизация (по умолчанию выключена: бот закрыт allowlist'ом).
# PUBLIC_MODE=true открывает бота внешним пользователям с квотами и подпиской.
PUBLIC_MODE=false
SUBSCRIPTION_PRICE_STARS=149
QUOTA_STARTER=3
QUOTA_FREE_WEEKLY=1
QUOTA_SUB_MONTHLY=30
# Ролик без субтитров длиннее этого порога списывает 2 единицы лимита.
HEAVY_DURATION_SEC=3600
```

- [ ] **Step 2: README**

Раздел «Монетизация (PUBLIC_MODE)» после раздела о премьерах: модель доступа (allowlist безлимит / 3 старт / 1 в нед / подписка 149 ⭐ = 30 в мес, тяжёлые ×2, кэш бесплатно), команды `/subscribe`, `/limits`, `/paysupport`, owner `/refund`; предупреждение: ДО включения PUBLIC_MODE перевести LLM на платный маршрут (`/llm_paid`), т.к. free-chain ненадёжен для платящих; лог-события `billing.*`.

- [ ] **Step 3: Финальная верификация**

```bash
./.venv/bin/pytest tests/ -q          # 67 passed
docker compose build
docker compose up -d && sleep 12 && docker compose logs --tail=30 bot | grep -E "billing.boot|Traceback"; docker compose down
```
Expected: `billing.boot public_mode=False ...`, без Traceback.

**Ручной чеклист owner'а (после включения PUBLIC_MODE, вне скоупа задач):** зайти с второго (не-allowlist) аккаунта → 3 стартовых → 4-я упирается в лимит с кнопкой → /subscribe → оплатить 149 ⭐ реальными звёздами → саммари проходит → /limits показывает остаток → /refund с owner-аккаунта возвращает платёж.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "Document Stars monetization: PUBLIC_MODE, quotas, payment commands"
```
