"""Q5: адаптивный max_tokens при обрезке вывода LLM (finish_reason=length).

Инцидент 2026-08-04: плотный длинный ролик требовал >2000 output-токенов на
partial-стадии; КАЖДАЯ free-модель в цепочке упиралась в наш же потолок
(finish_reason=length), truncation-guard браковал ответ и переключал модель —
3 прохода по цепочке, 20+ минут, ~20 сожжённых запросов дневного лимита.
Прыжки по моделям не лечат наш собственный лимит: прежде чем переключаться на
следующую модель, повторяем ТУ ЖЕ модель с бо́льшим max_tokens (одно
удвоение на модель за проход, ограничено ``max(original,
min(LLM_MAX_TOKENS_FINAL, 8000))``). См. app/llm_client.py
``OpenRouterClient._generate_with_adaptive_cap``.
"""

import httpx
import pytest

from app.config import load_settings
from app.db import Database
from app.llm_client import GenerationUsage, OpenRouterClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_FREE_CHAIN", "chain/model-1,chain/model-2")
    monkeypatch.setenv("OPENROUTER_MODEL_PAID", "chain/paid-model")
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_PASSES", "0")
    # Явный потолок для детерминированного расчёта cap в тестах.
    monkeypatch.setenv("LLM_MAX_TOKENS_FINAL", "8000")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


@pytest.fixture
def client_prod_passes(tmp_path, monkeypatch):
    """Как ``client``, но с прод-конфигом ретраев: retry_passes=2 (passes=3),
    5 моделей в цепочке — ровно репро ревьюера для мандатного short-circuit
    "все обрезались -> один проход"."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv(
        "OPENROUTER_MODEL_FREE_CHAIN",
        "chain/model-1,chain/model-2,chain/model-3,chain/model-4,chain/model-5",
    )
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_PASSES", "2")
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_DELAY_SEC", "30")
    monkeypatch.setenv("LLM_MAX_TOKENS_FINAL", "8000")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


def _completion(content: str, finish_reason: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _wire_by_model_and_tokens(monkeypatch, responder):
    """responder(model, max_tokens) -> dict (тело JSON) для мока POST."""
    calls: list[tuple[str, int]] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        max_tokens = json["max_tokens"]
        calls.append((model, max_tokens))
        body = responder(model, max_tokens)
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


# ── (a) обрезка на 2000 → повтор ТОЙ ЖЕ модели на 4000 → успех, model-2 не звался ──


async def test_truncated_retry_same_model_bigger_cap_succeeds(client, monkeypatch):
    def responder(model, max_tokens):
        assert model == "chain/model-1", "model-2 не должна вызываться — успех после удвоения"
        if max_tokens == 2000:
            return _completion("thinking thinking thinking", "length")
        assert max_tokens == 4000
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    usage = GenerationUsage()
    result = await client.generate("p", usage=usage, max_tokens=2000)

    assert result == '{"overview": "ок"}'
    assert calls == [("chain/model-1", 2000), ("chain/model-1", 4000)]
    assert usage.last_finish_reason == "stop"


# ── (b) обрезка и на удвоенном лимите → trying_next (следующая модель) ──


async def test_truncated_retry_still_truncated_moves_to_next_model(client, monkeypatch):
    def responder(model, max_tokens):
        if model == "chain/model-1":
            return _completion("loop loop", "length")  # брак и на 2000, и на 4000
        assert model == "chain/model-2"
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    result = await client.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    assert calls == [
        ("chain/model-1", 2000),
        ("chain/model-1", 4000),
        ("chain/model-2", 2000),
    ]


# ── (c) все модели обрезались (даже после удвоения) → last-resort за ОДИН проход ──


async def test_all_models_truncated_last_resort_after_one_pass(client, monkeypatch):
    async def empty_catalog(self, url, headers=None):
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", empty_catalog)

    def responder(model, max_tokens):
        return _completion(f"{model}@{max_tokens}", "length")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    usage = GenerationUsage()
    result = await client.generate("p", usage=usage, max_tokens=2000)

    # OPENROUTER_FALLBACK_RETRY_PASSES=0 → один проход, без сна между
    # проходами. Last-resort — от ПОСЛЕДНЕЙ (удвоенной, самой длинной)
    # попытки последней модели в цепочке.
    assert result == "chain/model-2@4000"
    assert calls == [
        ("chain/model-1", 2000),
        ("chain/model-1", 4000),
        ("chain/model-2", 2000),
        ("chain/model-2", 4000),
    ]
    assert usage.last_finish_reason == "length"


# ── (d) нетронутый путь: обычная ошибка → trying_next без удвоения ──


async def test_plain_error_does_not_trigger_doubling(client, monkeypatch):
    calls: list[tuple[str, int]] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append((model, json["max_tokens"]))
        if model == "chain/model-1":
            return httpx.Response(
                429, json={"error": {"code": 429}}, request=httpx.Request("POST", url)
            )
        return httpx.Response(
            200,
            json=_completion('{"overview": "ок"}', "stop"),
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await client.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    # model-1 вызвана РОВНО один раз — 429 не запускает удвоение, это не обрезка.
    assert calls == [("chain/model-1", 2000), ("chain/model-2", 2000)]


# ── нет запаса до потолка → ведёт себя как раньше (без удвоения) ──


async def test_truncated_at_cap_already_no_headroom_moves_to_next_model(client, monkeypatch):
    """max_tokens уже равен потолку (LLM_MAX_TOKENS_FINAL=8000) — удваивать
    нечего, немедленный trying_next, как до Q5."""

    def responder(model, max_tokens):
        if model == "chain/model-1":
            return _completion("loop", "length")
        assert model == "chain/model-2"
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    result = await client.generate("p", max_tokens=8000)

    assert result == '{"overview": "ок"}'
    assert calls == [("chain/model-1", 8000), ("chain/model-2", 8000)]


# ── Paid-путь: та же адаптивная логика при обрезке ──


async def test_paid_path_truncated_retries_with_bigger_cap(client, monkeypatch):
    client.set_paid_mode(True)

    def responder(model, max_tokens):
        assert model == "chain/paid-model"
        if max_tokens == 2000:
            return _completion("thinking...", "length")
        assert max_tokens == 4000
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    result = await client.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    assert calls == [("chain/paid-model", 2000), ("chain/paid-model", 4000)]


async def test_paid_path_truncated_at_cap_returns_last_resort_immediately(client, monkeypatch):
    """Платная модель без запаса до потолка — прежнее поведение: обрезанный
    текст отдаётся немедленно, без второго прохода (единственная модель, без
    цепочки — повторять нечего)."""
    client.set_paid_mode(True)
    calls = _wire_by_model_and_tokens(
        monkeypatch, lambda model, max_tokens: _completion("stuck", "length")
    )

    result = await client.generate("p", max_tokens=8000)

    assert result == "stuck"
    assert calls == [("chain/paid-model", 8000)]


# ── Ревью-фикс: прод-конфиг (retry_passes=2, 5 моделей) — тотальная обрезка ──
# должна остановиться на ОДНОМ проходе, а не крутить sleep+retry ещё 2 раза.
# Дискриминирующий тест: НЕ пинит retry_passes=0 (в отличие от фикстуры
# ``client`` выше), поэтому тавтологического "один проход" из конфига здесь
# нет — если short-circuit не сработает, тест поймает реальные 3 прохода.


async def test_all_truncated_prod_config_stops_after_one_pass(client_prod_passes, monkeypatch):
    calls: list[str] = []
    sleeps: list[float] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        return httpx.Response(
            200,
            json=_completion(f"{model}@{json['max_tokens']}", "length"),
            request=httpx.Request("POST", url),
        )

    async def empty_catalog(self, url, headers=None):
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", empty_catalog)
    monkeypatch.setattr("app.llm_client.asyncio.sleep", fake_sleep)

    usage = GenerationUsage()
    result = await client_prod_passes.generate("p", usage=usage, max_tokens=2000)

    # 5 моделей × 2 запроса (adaptive-cap: оригинал 2000 + удвоение 4000),
    # РОВНО один проход — не 3 (было бы 5*1*3=15 до Q5, 5*2*3=30 без
    # short-circuit; фикс держит это на 5*2*1=10).
    assert len(calls) == 10, calls
    assert calls == [
        "chain/model-1", "chain/model-1",
        "chain/model-2", "chain/model-2",
        "chain/model-3", "chain/model-3",
        "chain/model-4", "chain/model-4",
        "chain/model-5", "chain/model-5",
    ]
    # Ни одного sleep между проходами — цепочка не пошла на 2-й/3-й проход.
    assert sleeps == []
    # last_resort — от последней (удвоенной, самой длинной) попытки.
    assert result == "chain/model-5@4000"
    assert usage.last_finish_reason == "length"


async def test_mixed_pass_with_real_error_still_retries_next_pass(client_prod_passes, monkeypatch):
    """Regression: если В ТОМ ЖЕ проходе хоть одна модель дала настоящую (не
    truncation) ошибку, short-circuit не должен срабатывать — проходы
    продолжаются как раньше (настоящая ошибка может не повториться)."""
    calls: list[str] = []
    sleeps: list[float] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        if model == "chain/model-3":
            # Настоящая (не truncation) ошибка — держит проход "смешанным".
            return httpx.Response(
                429, json={"error": {"code": 429}}, request=httpx.Request("POST", url)
            )
        if model == "chain/model-5" and calls.count("chain/model-5") >= 3:
            # На третьем проходе модель-5 наконец отвечает нормально.
            return httpx.Response(
                200,
                json=_completion('{"overview": "ок"}', "stop"),
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json=_completion(f"{model}@{json['max_tokens']}", "length"),
            request=httpx.Request("POST", url),
        )

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.llm_client.asyncio.sleep", fake_sleep)

    result = await client_prod_passes.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    # Продолжались проходы (sleep случился между ними) — смешанный проход не
    # схлопнулся в один.
    assert len(sleeps) >= 1
