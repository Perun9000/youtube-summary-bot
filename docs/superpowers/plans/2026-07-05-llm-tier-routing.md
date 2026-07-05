# LLM Tier Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Платная OpenRouter-модель используется автоматически ТОЛЬКО для подписчиков и ТОЛЬКО когда free-цепочка недоступна (исчерпана/breaker) или медленна (>180 сек); бесплатные внешние пользователи никогда не тратят платные токены — даже при глобальном `/llm_paid`.

**Architecture:** Параметр `route: str = "default"` на `LLMClient.generate` ('default' | 'free_only' | 'paid_fallback'). `OpenRouterClient.generate` рефакторится: инлайновые free/paid ветки извлекаются в `_generate_free_chain` / `_generate_paid`, поверх — диспетчер по route; fallback-путь оборачивает free-этап в `asyncio.wait_for(budget)`. Summarizer хранит route на время `summarize()` (воркер последовательный); pipeline вычисляет route из `job.quota_user_id` + `billing.is_subscriber`.

**Tech Stack:** Python 3.11, существующие app/llm_client.py (aiogram-независим), pytest.

## Global Constraints

- Матрица маршрутизации (подтверждена владельцем):
  - `default` (allowlist/owner/scheduled/дайджест-ранжирование): текущее поведение — глобальный `/llm_paid` решает paid vs free-chain.
  - `free_only` (внешний БЕЗ подписки): ВСЕГДА free-цепочка, глобальный paid-режим игнорируется.
  - `paid_fallback` (внешний подписчик): при глобальном paid — сразу платная; иначе free-цепочка с бюджетом `PAID_FALLBACK_FREE_BUDGET_SEC=180` сек на весь free-этап → при TimeoutError / исчерпании цепочки / открытом breaker'е — платная модель.
- Бюджетная блокировка OpenRouter (`OPENROUTER_BUDGET_EXCEEDED_MARKER`) — стоп для ВСЕХ маршрутов, включая fallback (проверка до диспатча, как сейчас).
- Circuit breaker отражает здоровье free-цепочки: fallback-платная попытка НЕ вызывает `record_success` (paid-успех ничего не говорит о free-цепочке); `record_failure` при исчерпании free-цепочки сохраняется, даже если платная спасла. Пути default/global-paid не меняют текущую breaker-семантику.
- Поведение маршрута `default` байт-в-байт эквивалентно текущему (существующие вызовы без route не меняют поведения).
- LMStudioClient принимает `route` и игнорирует его.
- Тексты/комментарии русские; suite сейчас 68/68, после плана 77/77; вывод pytest чистый.
- Коммиты английские, в конце `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Маршрутизация в OpenRouterClient

**Files:**
- Modify: `app/llm_client.py` (Protocol `LLMClient.generate`, `OpenRouterClient.generate` + новые `_generate_free_chain`/`_generate_paid`/`_generate_with_paid_fallback`, `LMStudioClient.generate`), `app/config.py`
- Test: `tests/test_llm_routing.py`, `tests/test_config.py` (+1)

**Interfaces:**
- Consumes: текущий `generate()` (app/llm_client.py:376-450): budget check → breaker check → paid-ветка (`_generate_with_retries(chain[0], ...)` + `record_success`) / free-ветка (циклы passes×chain c `_generate_one_attempt`, `record_success` при успехе, финальный `record_failure` + RuntimeError c текстом «все free-модели…»).
- Produces:
  - `LLMClient.generate(self, prompt, system=None, usage=None, max_tokens=None, route: str = "default") -> str` (Protocol + обе реализации).
  - `OpenRouterClient._generate_free_chain(prompt, system, usage, max_tokens) -> str` — извлечённая free-ветка (тела циклов НЕ менять: тот же логгинг, тот же record_success/record_failure, тот же финальный RuntimeError).
  - `OpenRouterClient._generate_paid(prompt, system, usage, max_tokens, *, record_success: bool = True) -> str` — `_generate_with_retries(self._settings.openrouter_model_paid, ...)`; `record_success()` только при `record_success=True`.
  - `OpenRouterClient._generate_with_paid_fallback(prompt, system, usage, max_tokens) -> str`.
  - `Settings.paid_fallback_free_budget_sec: int` (env `PAID_FALLBACK_FREE_BUDGET_SEC`, default "180").

- [ ] **Step 1: Failing tests**

`tests/test_config.py` — добавить:

```python
def test_paid_fallback_budget_default(base_env):
    assert load_settings().paid_fallback_free_budget_sec == 180
```

`tests/test_llm_routing.py`:

```python
import asyncio

import pytest

from app.config import load_settings
from app.db import Database
from app.llm_client import OPENROUTER_BUDGET_EXCEEDED_MARKER, OpenRouterClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # не подхватывать реальный .env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_PAID", "paid/model")
    monkeypatch.setenv("PAID_FALLBACK_FREE_BUDGET_SEC", "1")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


def wire(client, monkeypatch, *, free, paid):
    """Подменить free/paid этапы фейками, записывающими порядок вызовов."""
    calls: list[str] = []

    async def fake_free(prompt, system, usage, max_tokens):
        calls.append("free")
        return await free()

    async def fake_paid(prompt, system, usage, max_tokens, *, record_success=True):
        calls.append("paid")
        return await paid()

    monkeypatch.setattr(client, "_generate_free_chain", fake_free)
    monkeypatch.setattr(client, "_generate_paid", fake_paid)
    return calls


async def ok_free():
    return "free-result"


async def ok_paid():
    return "paid-result"


async def test_free_only_ignores_global_paid_mode(client, monkeypatch):
    client.set_paid_mode(True)
    calls = wire(client, monkeypatch, free=ok_free, paid=ok_paid)
    assert await client.generate("p", route="free_only") == "free-result"
    assert calls == ["free"]


async def test_default_paid_mode_goes_paid(client, monkeypatch):
    client.set_paid_mode(True)
    calls = wire(client, monkeypatch, free=ok_free, paid=ok_paid)
    assert await client.generate("p") == "paid-result"
    assert calls == ["paid"]


async def test_paid_fallback_fast_free_no_paid(client, monkeypatch):
    calls = wire(client, monkeypatch, free=ok_free, paid=ok_paid)
    assert await client.generate("p", route="paid_fallback") == "free-result"
    assert calls == ["free"]


async def test_paid_fallback_on_free_exhaustion(client, monkeypatch):
    async def dead_free():
        raise RuntimeError("OpenRouter: все free-модели в цепочке отказались отвечать")

    calls = wire(client, monkeypatch, free=dead_free, paid=ok_paid)
    assert await client.generate("p", route="paid_fallback") == "paid-result"
    assert calls == ["free", "paid"]


async def test_paid_fallback_on_slow_free(client, monkeypatch):
    async def slow_free():
        await asyncio.sleep(5)  # бюджет в фикстуре — 1 сек
        return "free-result"

    calls = wire(client, monkeypatch, free=slow_free, paid=ok_paid)
    assert await client.generate("p", route="paid_fallback") == "paid-result"
    assert calls == ["free", "paid"]


async def test_paid_fallback_breaker_open_goes_straight_paid(client, monkeypatch):
    calls = wire(client, monkeypatch, free=ok_free, paid=ok_paid)
    client._breaker.record_failure()
    client._breaker.record_failure()  # threshold=2 → открыт
    assert await client.generate("p", route="paid_fallback") == "paid-result"
    assert calls == ["paid"]


async def test_paid_fallback_budget_error_propagates(client, monkeypatch):
    async def budget_dead_free():
        raise RuntimeError(f"{OPENROUTER_BUDGET_EXCEEDED_MARKER}: daily cap")

    calls = wire(client, monkeypatch, free=budget_dead_free, paid=ok_paid)
    with pytest.raises(RuntimeError, match=OPENROUTER_BUDGET_EXCEEDED_MARKER):
        await client.generate("p", route="paid_fallback")
    assert calls == ["free"]


async def test_paid_fallback_global_paid_goes_straight_paid(client, monkeypatch):
    client.set_paid_mode(True)
    calls = wire(client, monkeypatch, free=ok_free, paid=ok_paid)
    assert await client.generate("p", route="paid_fallback") == "paid-result"
    assert calls == ["paid"]
```

Run: `./.venv/bin/pytest tests/test_llm_routing.py tests/test_config.py -q` — Expected: FAIL (нет поля/атрибутов).

Примечание к тестам: `test_paid_fallback_budget_error_propagates` использует free-фейк с маркером бюджета — это проверяет пробрасывание маркера из free-этапа fallback-пути; глобальная бюджетная блокировка до диспатча остаётся как есть (покрыта существующим поведением, отдельного теста не требует).

- [ ] **Step 2: config.py**

`Settings`: поле `paid_fallback_free_budget_sec: int` (рядом с `premiere_delay_hours`). В hoisted-блоке `load_settings`:

```python
    # Tier-маршрутизация LLM: сколько секунд подписчик ждёт free-цепочку,
    # прежде чем запрос уйдёт на платную модель (маршрут paid_fallback).
    paid_fallback_free_budget_sec = env.int("PAID_FALLBACK_FREE_BUDGET_SEC", "180")
```
и передать в `Settings(...)`.

- [ ] **Step 3: Рефакторинг llm_client.py**

1. Protocol `LLMClient.generate` (строка ~95) и `LMStudioClient.generate` (~726): добавить параметр `route: str = "default"` (LMStudio его игнорирует; докстрока: «локальная модель бесплатна — маршрутизация не применяется»).
2. Извлечь из `OpenRouterClient.generate` free-ветку (строки 405-450: циклы passes×chain, финальный error c suffix'ом и `record_failure`) в `_generate_free_chain(self, prompt, system, usage, max_tokens) -> str` — тело переносится БЕЗ изменений.
3. Новый `_generate_paid`:

```python
    async def _generate_paid(
        self,
        prompt: str,
        system: str | None,
        usage: GenerationUsage | None,
        max_tokens: int | None,
        *,
        record_success: bool = True,
    ) -> str:
        """Одна платная модель со стандартной retry-политикой.

        record_success=False — для fallback-пути подписчика: успех платной
        модели ничего не говорит о здоровье free-цепочки, breaker не трогаем.
        """
        result = await self._generate_with_retries(
            self._settings.openrouter_model_paid, prompt, system, usage, max_tokens
        )
        if record_success:
            self._breaker.record_success()
        return result
```

4. Новый `generate`:

```python
    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        usage: GenerationUsage | None = None,
        max_tokens: int | None = None,
        route: str = "default",
    ) -> str:
        ok, reason = self._budget.check()
        if not ok:
            logger.warning("llm.generate.budget_block provider=openrouter reason=%s", reason)
            raise RuntimeError(f"{OPENROUTER_BUDGET_EXCEEDED_MARKER}: {reason}")

        if route == "paid_fallback":
            return await self._generate_with_paid_fallback(prompt, system, usage, max_tokens)

        if self._breaker.is_open():
            raise RuntimeError(
                "OpenRouter временно недоступен (circuit breaker), "
                f"следующая попытка через ~{int(self._breaker.remaining_sec() / 60) + 1} мин."
            )

        # free_only (внешний без подписки): всегда free-цепочка, глобальный
        # /llm_paid игнорируется — платные токены только платящим.
        if route != "free_only" and self.is_paid_mode():
            if not self._settings.openrouter_model_paid:
                raise RuntimeError("OpenRouter: платная модель не задана. Проверь .env.")
            return await self._generate_paid(prompt, system, usage, max_tokens)

        if not self._settings.openrouter_model_free_chain:
            raise RuntimeError("OpenRouter: список моделей пуст. Проверь .env.")
        return await self._generate_free_chain(prompt, system, usage, max_tokens)
```

(Проверка `current_chain()` из старого кода распадается на две ветки выше; `current_chain()` как метод остаётся — им пользуются /model-команды.)

5. Fallback-путь:

```python
    async def _generate_with_paid_fallback(
        self,
        prompt: str,
        system: str | None,
        usage: GenerationUsage | None,
        max_tokens: int | None,
    ) -> str:
        """Маршрут подписчика: free сначала, платная — когда free плоха.

        Триггеры платной попытки: открытый breaker (free-цепочка лежит),
        исчерпание цепочки, превышение бюджета времени
        PAID_FALLBACK_FREE_BUDGET_SEC на весь free-этап. Бюджетная ошибка
        OpenRouter (дневной cap) пробрасывается — это стоп для всех маршрутов.
        """
        paid_model = self._settings.openrouter_model_paid
        if not paid_model:
            # Фолбэчить не на что — ведём себя как free_only.
            return await self._generate_free_chain(prompt, system, usage, max_tokens)

        if self.is_paid_mode():
            # Владелец включил платный режим глобально — подписчик тоже сразу
            # на платной (это его tier).
            return await self._generate_paid(prompt, system, usage, max_tokens)

        if self._breaker.is_open():
            logger.info("llm.paid_fallback.trigger reason=breaker_open")
        else:
            budget_sec = self._settings.paid_fallback_free_budget_sec
            try:
                return await asyncio.wait_for(
                    self._generate_free_chain(prompt, system, usage, max_tokens),
                    timeout=budget_sec,
                )
            except asyncio.TimeoutError:
                logger.info(
                    "llm.paid_fallback.trigger reason=slow budget_sec=%s", budget_sec
                )
            except RuntimeError as exc:
                if OPENROUTER_BUDGET_EXCEEDED_MARKER in str(exc):
                    raise
                # Цепочка исчерпана; record_failure уже сделан внутри —
                # breaker честно отражает здоровье free для остальных.
                logger.info("llm.paid_fallback.trigger reason=free_exhausted")

        result = await self._generate_paid(
            prompt, system, usage, max_tokens, record_success=False
        )
        logger.info("llm.paid_fallback.success model=%s", paid_model)
        return result
```

- [ ] **Step 4: Прогнать тесты**

`./.venv/bin/pytest tests/ -q` — Expected: 77 passed (68 + 8 routing + 1 config).

- [ ] **Step 5: Commit**

```bash
git add app/llm_client.py app/config.py tests/test_llm_routing.py tests/test_config.py
git commit -m "LLM tier routing: free_only / paid_fallback routes with slow-free budget"
```

---

### Task 2: Прокладка route через Summarizer и pipeline + документация

**Files:**
- Modify: `app/summarizer.py`, `app/pipeline.py`, `.env.example`, `README.md`
- Test: `tests/test_llm_routing.py` (+1)

**Interfaces:**
- Consumes: `LLMClient.generate(..., route=...)` (Task 1), `SummaryJob.quota_user_id`, `services.billing.is_subscriber(user_id)`.
- Produces: `Summarizer.summarize(..., llm_route: str = "default")`.

- [ ] **Step 1: Failing test**

В `tests/test_llm_routing.py` добавить:

```python
from app.summarizer import Summarizer


class _RouteRecordingLLM:
    def __init__(self):
        self.routes: list[str] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    async def generate(self, prompt, system=None, usage=None, max_tokens=None, route="default"):
        self.routes.append(route)
        return '{"overview": "x", "chapters": [], "tags": {}}'


async def test_summarizer_threads_route_to_llm():
    llm = _RouteRecordingLLM()
    summarizer = Summarizer(llm, system_prompt_provider=lambda: "sys")
    await summarizer.summarize(
        url="https://youtu.be/x", title="t", chunks=["один чанк"],
        llm_route="paid_fallback",
    )
    assert llm.routes and all(r == "paid_fallback" for r in llm.routes)
```

(Сигнатуру конструктора Summarizer сверь с фактической — обязательные kwargs передай минимально необходимые; если summarize требует progress/usage — передай реальные `SummaryProgress()`/`GenerationUsage()`.)

Run — FAIL (`unexpected keyword argument 'llm_route'`).

- [ ] **Step 2: Summarizer**

В `Summarizer.__init__` добавить `self._route: str = "default"`. В начале `summarize(...)` — новый параметр `llm_route: str = "default"` и строка `self._route = llm_route` с комментарием:

```python
        # Маршрут LLM на время этой суммаризации. Инстанс-атрибут безопасен:
        # summary-воркер строго последовательный, конкурирующих summarize нет.
        self._route = llm_route
```

Все 7 вызовов `self._llm.generate(...)` (строки ~325, 337, 387, 425, 464, 481, 502) — добавить `route=self._route`.

- [ ] **Step 3: pipeline.py**

В `_process_youtube_job`, рядом с существующей подготовкой к суммаризации (перед `services.summarizer.summarize(...)`):

```python
        # Tier-маршрутизация LLM: подписчик получает paid-fallback (free
        # сначала, платная при недоступности/медленности); бесплатный внешний
        # пользователь никогда не тратит платные токены; allowlist — как
        # раньше, по глобальному /llm_paid.
        llm_route = "default"
        if job.quota_user_id is not None:
            is_sub = bool(
                services.billing is not None
                and services.billing.is_subscriber(job.quota_user_id)
            )
            llm_route = "paid_fallback" if is_sub else "free_only"
            logger.info("job.llm_route job_id=%s route=%s", job_id, llm_route)
```

и `llm_route=llm_route` в вызов `summarize(...)`.

- [ ] **Step 4: Документация**

`.env.example` (в блок монетизации):

```dotenv
# Подписчик ждёт free-цепочку не дольше этого бюджета (сек) — дальше запрос
# уходит на платную модель OpenRouter. Free-пользователи платную не используют.
PAID_FALLBACK_FREE_BUDGET_SEC=180
```

`README.md`, раздел «Монетизация (PUBLIC_MODE)» — подраздел «Маршрутизация LLM по тарифам»: таблица матрицы (allowlist → глобальный /llm_paid; подписчик → free с бюджетом 180 сек, затем платная при slow/exhausted/breaker; free-внешний → только free-цепочка всегда); лог-события `job.llm_route`, `llm.paid_fallback.trigger`, `llm.paid_fallback.success`. Убрать/смягчить прежнюю рекомендацию «включи /llm_paid перед PUBLIC_MODE» — теперь она не обязательна: подписчики защищены fallback'ом автоматически (оставить как опцию для скорости).

- [ ] **Step 5: Прогнать всё + верификация**

```bash
./.venv/bin/pytest tests/ -q          # 78 passed
python3 -m compileall app/ -q
docker compose build
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Thread LLM route through summarizer and pipeline; document tier routing"
```
