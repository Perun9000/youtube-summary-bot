# Dynamic Free-Chain Tail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Когда сконфигурированная free-цепочка OpenRouter исчерпана, пробовать до 3 живых free-моделей из актуального каталога перед тем, как ронять job.

**Architecture:** Чистая функция-селектор фильтрует каталог `GET /models` (кэш в памяти, TTL 1 час); `_generate_free_chain` после исчерпания всех проходов делает один проход по отобранному хвосту той же `_generate_one_attempt`. Ошибка каталога/пустой хвост → прежнее поведение.

**Tech Stack:** Python 3.11, httpx, pytest (asyncio_mode=auto), monkeypatch httpx.AsyncClient.

**Спека:** `docs/superpowers/specs/2026-07-14-dynamic-free-chain-tail-design.md`

## Global Constraints

- Хвост: максимум 3 модели; фильтр-подстроки `coder`, `code`, `safety`, `vl`, `vision`, `guard` (case-insensitive, по части id после `/`); `context_length >= 131072`; сортировка по убыванию `context_length`.
- TTL кэша каталога: 3600 сек (`time.monotonic`).
- Breaker хвост не трогает вообще (ни success, ни дополнительный failure).
- Тесты запускать через `.venv/bin/python -m pytest`.

---

### Task 1: Чистый селектор хвоста

**Files:**
- Modify: `app/llm_client.py` (модульные константы + функция рядом с `_strip_thinking`)
- Test: `tests/test_dynamic_tail.py` (новый)

**Interfaces:**
- Produces: `_select_dynamic_tail(catalog: list[dict], exclude_ids: set[str]) -> list[str]` — id моделей хвоста по приоритету; константы `DYNAMIC_TAIL_MAX_MODELS = 3`, `DYNAMIC_TAIL_MIN_CONTEXT = 131072`, `DYNAMIC_TAIL_EXCLUDE_SUBSTRINGS`, `MODELS_CATALOG_TTL_SEC = 3600`.

- [ ] **Step 1: Write the failing tests**

```python
"""Динамический хвост free-цепочки: отбор моделей из каталога OpenRouter."""

from app.llm_client import _select_dynamic_tail


def _entry(model_id: str, ctx: int) -> dict:
    return {"id": model_id, "context_length": ctx}


CATALOG = [
    _entry("qwen/qwen3-coder:free", 1048576),          # coder — вон
    _entry("nvidia/nemotron-3.5-content-safety:free", 128000),  # safety — вон
    _entry("nvidia/nemotron-nano-12b-v2-vl:free", 128000),      # vl — вон
    _entry("cohere/north-mini-code:free", 256000),      # code — вон
    _entry("dolphin/tiny-guard:free", 200000),          # guard — вон
    _entry("meta-llama/llama-3.2-3b-instruct:free", 32768),     # маленький ctx — вон
    _entry("qwen/qwen3-next-80b-a3b-instruct:free", 262144),    # в цепочке — вон
    _entry("vendor/paid-model", 262144),                # не :free — вон
    _entry("poolside/laguna-m.1:free", 262144),
    _entry("google/gemma-4-31b-it:free", 262144),
    _entry("tencent/hy3:free", 262144),
    _entry("nousresearch/hermes-3-llama-3.1-405b:free", 131072),
]


def test_selector_filters_and_orders():
    tail = _select_dynamic_tail(
        CATALOG, exclude_ids={"qwen/qwen3-next-80b-a3b-instruct:free"}
    )
    # Только универсальные chat-модели, отсортированы по ctx, максимум 3.
    assert len(tail) == 3
    assert set(tail) <= {
        "poolside/laguna-m.1:free",
        "google/gemma-4-31b-it:free",
        "tencent/hy3:free",
    }


def test_selector_empty_catalog():
    assert _select_dynamic_tail([], exclude_ids=set()) == []


def test_selector_orders_by_context_desc():
    catalog = [
        _entry("a/model-one:free", 131072),
        _entry("b/model-two:free", 500000),
        _entry("c/model-three:free", 262144),
    ]
    tail = _select_dynamic_tail(catalog, exclude_ids=set())
    assert tail == ["b/model-two:free", "c/model-three:free", "a/model-one:free"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_dynamic_tail.py -q`
Expected: FAIL / collection error — `_select_dynamic_tail` не существует.

- [ ] **Step 3: Write minimal implementation**

В `app/llm_client.py`, рядом с прочими модульными константами:

```python
# Динамический хвост free-цепочки: когда сконфигурированные модели исчерпаны,
# пробуем ещё несколько живых free-моделей из каталога (см. спеку
# docs/superpowers/specs/2026-07-14-dynamic-free-chain-tail-design.md).
DYNAMIC_TAIL_MAX_MODELS = 3
DYNAMIC_TAIL_MIN_CONTEXT = 131072
DYNAMIC_TAIL_EXCLUDE_SUBSTRINGS = ("coder", "code", "safety", "vl", "vision", "guard")
MODELS_CATALOG_TTL_SEC = 3600
```

Функция на уровне модуля (рядом с `_strip_thinking`):

```python
def _select_dynamic_tail(catalog: list[dict], exclude_ids: set[str]) -> list[str]:
    """Отобрать free-модели для динамического хвоста.

    Только универсальные chat-модели: спец-модели (код, safety, vision)
    и маленький контекст дают корявые русские саммари, guards от которых
    не спасают (валидный JSON с плохим текстом публикуется).
    """
    candidates: list[tuple[int, str]] = []
    for entry in catalog:
        model_id = str(entry.get("id", ""))
        if not model_id.endswith(":free") or model_id in exclude_ids:
            continue
        name_part = model_id.split("/", 1)[-1].lower()
        if any(bad in name_part for bad in DYNAMIC_TAIL_EXCLUDE_SUBSTRINGS):
            continue
        try:
            context = int(entry.get("context_length") or 0)
        except (TypeError, ValueError):
            context = 0
        if context < DYNAMIC_TAIL_MIN_CONTEXT:
            continue
        candidates.append((context, model_id))
    candidates.sort(key=lambda pair: -pair[0])
    return [model_id for _, model_id in candidates[:DYNAMIC_TAIL_MAX_MODELS]]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_dynamic_tail.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/llm_client.py tests/test_dynamic_tail.py
git commit -m "Add dynamic-tail model selector for the free chain"
```

---

### Task 2: Каталог моделей с TTL-кэшем

**Files:**
- Modify: `app/llm_client.py` (класс `OpenRouterClient`: `__init__`, новый метод)
- Test: `tests/test_dynamic_tail.py`

**Interfaces:**
- Consumes: `_select_dynamic_tail` (Task 1), существующие `self._headers()`, `_raise_for_status`.
- Produces: `async OpenRouterClient._catalog_models() -> list[dict]` — сырые записи каталога; поля инстанса `_catalog_cache: list[dict] | None`, `_catalog_fetched_at: float`.

- [ ] **Step 1: Write the failing test**

Дополнить `tests/test_dynamic_tail.py` (фикстура `client` — копия из
`tests/test_truncated_output.py`, вынести в начало файла):

```python
import httpx
import pytest

from app.config import load_settings
from app.db import Database
from app.llm_client import OpenRouterClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_FREE_CHAIN", "chain/model-1,chain/model-2")
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_PASSES", "0")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


def _wire_catalog(monkeypatch, models: list[dict], counter: list[int]):
    async def fake_get(self, url, headers=None):
        counter[0] += 1
        return httpx.Response(
            200, json={"data": models}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_catalog_cached_within_ttl(client, monkeypatch):
    counter = [0]
    _wire_catalog(monkeypatch, [_entry("x/model:free", 200000)], counter)
    first = await client._catalog_models()
    second = await client._catalog_models()
    assert counter[0] == 1
    assert first == second == [{"id": "x/model:free", "context_length": 200000}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_dynamic_tail.py::test_catalog_cached_within_ttl -q`
Expected: FAIL — `_catalog_models` не существует.

- [ ] **Step 3: Write minimal implementation**

В `OpenRouterClient.__init__` добавить:

```python
        self._catalog_cache: list[dict] | None = None
        self._catalog_fetched_at = 0.0
```

Новый метод (рядом с `list_models`):

```python
    async def _catalog_models(self) -> list[dict]:
        """Сырой каталог /models с in-memory кэшем на MODELS_CATALOG_TTL_SEC.

        TTL защищает каталог от долбёжки, когда во время шторма 429
        цепочка исчерпывается на каждом job'е подряд.
        """
        now = time.monotonic()
        if (
            self._catalog_cache is not None
            and now - self._catalog_fetched_at < MODELS_CATALOG_TTL_SEC
        ):
            return self._catalog_cache
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self._settings.openrouter_base_url}/models",
                headers=self._headers(),
            )
            _raise_for_status(response, "OpenRouter")
            data = response.json()
        models = [m for m in data.get("data", []) if isinstance(m, dict)]
        self._catalog_cache = models
        self._catalog_fetched_at = now
        return models
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_dynamic_tail.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/llm_client.py tests/test_dynamic_tail.py
git commit -m "Cache OpenRouter model catalog with 1h TTL"
```

---

### Task 3: Вплести хвост в _generate_free_chain

**Files:**
- Modify: `app/llm_client.py` (`_generate_free_chain`: между циклом проходов и last-resort/raise)
- Test: `tests/test_dynamic_tail.py`

**Interfaces:**
- Consumes: `_select_dynamic_tail`, `_catalog_models`, `_generate_one_attempt`, `_OpenRouterRetriable`, `_OpenRouterTruncated`.
- Produces: новое поведение `generate()` — видимых сигнатур не меняет.

- [ ] **Step 1: Write the failing tests**

Дополнить `tests/test_dynamic_tail.py`:

```python
from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER


def _completion(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _wire_posts(monkeypatch, responses_by_model: dict, calls: list[str]):
    """POST-ответы по payload['model']; значение — httpx.Response или int (код)."""

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        spec = responses_by_model[model]
        if isinstance(spec, int):
            return httpx.Response(
                spec, json={"error": {"code": spec}}, request=httpx.Request("POST", url)
            )
        return httpx.Response(200, json=spec, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def test_tail_rescues_exhausted_chain(client, monkeypatch):
    counter = [0]
    _wire_catalog(monkeypatch, [_entry("tail/rescue-model:free", 200000)], counter)
    calls: list[str] = []
    _wire_posts(
        monkeypatch,
        {
            "chain/model-1": 429,
            "chain/model-2": 429,
            "tail/rescue-model:free": _completion('{"overview": "ок"}'),
        },
        calls,
    )
    result = await client.generate("p")
    assert result == '{"overview": "ок"}'
    assert calls == ["chain/model-1", "chain/model-2", "tail/rescue-model:free"]


async def test_tail_failure_raises_exhausted_with_tail_note(client, monkeypatch):
    counter = [0]
    _wire_catalog(monkeypatch, [_entry("tail/rescue-model:free", 200000)], counter)
    calls: list[str] = []
    _wire_posts(
        monkeypatch,
        {
            "chain/model-1": 429,
            "chain/model-2": 429,
            "tail/rescue-model:free": 429,
        },
        calls,
    )
    with pytest.raises(RuntimeError, match=FREE_CHAIN_EXHAUSTED_MARKER) as exc_info:
        await client.generate("p")
    assert "хвост" in str(exc_info.value)
    assert calls[-1] == "tail/rescue-model:free"


async def test_catalog_error_keeps_old_behavior(client, monkeypatch):
    async def broken_get(self, url, headers=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "get", broken_get)
    calls: list[str] = []
    _wire_posts(monkeypatch, {"chain/model-1": 429, "chain/model-2": 429}, calls)
    with pytest.raises(RuntimeError, match=FREE_CHAIN_EXHAUSTED_MARKER):
        await client.generate("p")


async def test_tail_not_used_when_chain_alive(client, monkeypatch):
    counter = [0]
    _wire_catalog(monkeypatch, [_entry("tail/rescue-model:free", 200000)], counter)
    calls: list[str] = []
    _wire_posts(monkeypatch, {"chain/model-1": _completion('{"a": 1}')}, calls)
    result = await client.generate("p")
    assert result == '{"a": 1}'
    assert calls == ["chain/model-1"]
    assert counter[0] == 0  # каталог даже не запрашивали
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dynamic_tail.py -q`
Expected: test_tail_rescues_exhausted_chain и test_tail_failure_raises_exhausted_with_tail_note FAIL (цепочка бросает исключение до хвоста); test_catalog_error_keeps_old_behavior и test_tail_not_used_when_chain_alive PASS.

- [ ] **Step 3: Write implementation**

В `_generate_free_chain`, после цикла `for pass_idx ...` и ПЕРЕД блоком
`if truncated_text is not None:` вставить:

```python
        # Сконфигурированная цепочка исчерпана — последний рубеж: до
        # DYNAMIC_TAIL_MAX_MODELS живых free-моделей из каталога. Breaker
        # хвост не трогает: его успех ничего не говорит о здоровье цепочки.
        tail_note = "хвост пропущен"
        try:
            catalog = await self._catalog_models()
        except Exception as exc:  # noqa: BLE001 — хвост не новая точка отказа
            logger.warning(
                "llm.generate.dynamic_tail.skipped reason=catalog_error error=%s", exc
            )
        else:
            tail = _select_dynamic_tail(catalog, exclude_ids=set(chain))
            if not tail:
                logger.info("llm.generate.dynamic_tail.skipped reason=empty")
            for model in tail:
                logger.warning("llm.generate.dynamic_tail.try model=%s", model)
                try:
                    result = await self._generate_one_attempt(
                        model, prompt, system, usage, max_tokens
                    )
                    logger.info("llm.generate.dynamic_tail.success model=%s", model)
                    return result
                except _OpenRouterRetriable as exc:
                    last_error = exc.cause
                    if isinstance(exc, _OpenRouterTruncated):
                        truncated_text = exc.text
                    continue
            if tail:
                tail_note = f"динамический хвост тоже отказал ({len(tail)} моделей)"
```

И в текст финального RuntimeError добавить `tail_note`:

```python
        raise RuntimeError(
            f"{FREE_CHAIN_EXHAUSTED_MARKER}: все free-модели в цепочке отказались отвечать "
            f"за {passes} проходов ({models_tried}); {tail_note}. "
            f"Последняя ошибка: {last_error}. {suffix}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dynamic_tail.py -q`
Expected: 8 passed.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: все тесты зелёные (было 172 + 8 новых = 180).

- [ ] **Step 6: Commit**

```bash
git add app/llm_client.py tests/test_dynamic_tail.py
git commit -m "Try dynamic tail of catalog free models when the chain is exhausted"
```

---

### Task 4: Выкат и проверка

**Files:** нет изменений кода.

- [ ] **Step 1: Проверить, что очередь пуста**

Run: `sqlite3 data/bot.db "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','active');"`
Expected: 0 (иначе дождаться).

- [ ] **Step 2: Пересобрать контейнер**

Run: `docker compose up -d --build`
Expected: Container ... Started.

- [ ] **Step 3: Проверить boot**

Run: `docker logs youtube-summary-bot-bot-1 --since 1m 2>&1 | grep -E "billing.boot|ERROR|Traceback"`
Expected: строка billing.boot, без ошибок.
