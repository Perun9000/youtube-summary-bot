"""Q8 (часть 1): HTTP 200 с пустым/пробельным content — отказ модели, не успех.

Инцидент 2026-08-13: nemotron вернул HTTP 200 с ПУСТЫМ телом (0 chars) после
~5 минут ожидания. До этого фикса цепочка считала это успехом (finish_reason
!= "length" — truncation-guard не срабатывал) — downstream JSON-парсер падал
с raw_chars=0. Теперь пустой/пробельный content (при finish_reason != "length")
классифицируется как отказ модели: trying_next на следующую модель/попытку,
и НИКОГДА не last-resort кандидат (в отличие от _OpenRouterTruncated —
непустой обрезанный текст всё ещё может быть полезен парсеру).

Edge-case (finish_reason=length И content пуст — reasoning выжрал весь
лимит) классифицируется как truncated, не empty_response: лестница Q5/Q7
(bigger cap) даёт модели ещё один шанс дотянуться до реального вывода. Но
если она пустая ДАЖЕ на потолке — это тоже не last-resort кандидат (см.
test_all_models_length_truncated_but_empty_is_not_last_resort).
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
    monkeypatch.setenv("LLM_MAX_TOKENS_FINAL", "1200")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


@pytest.fixture
def client_high_cap(tmp_path, monkeypatch):
    """Как ``client``, но с запасом до потолка (LLM_MAX_TOKENS_FINAL=8000 >
    старт 1200) — нужно для проверки лестницы Q5/Q7 на reasoning-edge
    (finish_reason=length с пустым content), см.
    test_length_with_empty_content_gets_ladder_retry_same_model."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_FREE_CHAIN", "chain/model-1,chain/model-2")
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_PASSES", "0")
    monkeypatch.setenv("LLM_MAX_TOKENS_FINAL", "8000")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


def _wire_responses(monkeypatch, responses_by_model: dict[str, dict]):
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        body = responses_by_model[model]
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def _empty_catalog(monkeypatch):
    async def empty_catalog(self, url, headers=None):
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", empty_catalog)


def _completion(content: str, finish_reason: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


# ── (a) одна модель пустая (HTTP 200, "") → trying_next на следующую ───────


async def test_empty_response_falls_through_to_next_model(client, monkeypatch, caplog):
    calls = _wire_responses(
        monkeypatch,
        {
            "chain/model-1": _completion("", "stop"),
            "chain/model-2": _completion('{"overview": "ок"}', "stop"),
        },
    )
    usage = GenerationUsage()
    import logging

    with caplog.at_level(logging.WARNING, logger="app.llm_client"):
        result = await client.generate("p", usage=usage)

    assert result == '{"overview": "ок"}'
    assert calls == ["chain/model-1", "chain/model-2"]
    assert "reason=empty_response" in caplog.text


async def test_whitespace_only_response_is_treated_as_empty(client, monkeypatch):
    """Пробельный content (пробелы/переводы строк без реального текста) —
    тот же отказ, что и полностью пустая строка."""
    calls = _wire_responses(
        monkeypatch,
        {
            "chain/model-1": _completion("   \n\t  ", "stop"),
            "chain/model-2": _completion('{"overview": "ок"}', "stop"),
        },
    )
    result = await client.generate("p")
    assert result == '{"overview": "ок"}'
    assert calls == ["chain/model-1", "chain/model-2"]


# ── (b) ВСЕ модели пустые → chain exhausted, а не «успех с пустотой» ──────


async def test_all_models_empty_raises_chain_exhausted_not_empty_success(client, monkeypatch):
    _empty_catalog(monkeypatch)
    calls = _wire_responses(
        monkeypatch,
        {
            "chain/model-1": _completion("", "stop"),
            "chain/model-2": _completion("", "stop"),
        },
    )
    with pytest.raises(RuntimeError) as exc_info:
        await client.generate("p")

    from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER

    assert FREE_CHAIN_EXHAUSTED_MARKER in str(exc_info.value)
    assert calls == ["chain/model-1", "chain/model-2"]


# ── last_resort не деградировал: пустышка никогда не last-resort кандидат ──


async def test_empty_response_never_becomes_last_resort_when_mixed_with_truncated(
    client, monkeypatch
):
    """Смешанный случай: одна модель обрезалась (непустой truncated-текст —
    легитимный last-resort кандидат), другая вернула чистую пустоту. Цепочка
    должна вернуть обрезанный текст первой модели, а не пустышку второй —
    и не запутаться в том, какая ошибка "последняя"."""
    _empty_catalog(monkeypatch)
    calls = _wire_responses(
        monkeypatch,
        {
            "chain/model-1": _completion("some partial thinking text", "length"),
            "chain/model-2": _completion("", "stop"),
        },
    )
    result = await client.generate("p")
    assert result == "some partial thinking text"
    assert calls == ["chain/model-1", "chain/model-2"]


# ── reasoning-edge: finish_reason=length И content пуст → truncated (лестница
# получает шанс), но если он пустой ДАЖЕ на потолке — не last-resort кандидат


async def test_length_with_empty_content_gets_ladder_retry_same_model(
    client_high_cap, monkeypatch
):
    """finish_reason=length + content="" — классифицируем как truncated, не
    empty_response: лестница Q5/Q7 (bigger cap) пробует ту же модель с
    бо́льшим потолком, а не сразу прыгает на следующую модель."""

    def responder(model, max_tokens):
        assert model == "chain/model-1", "должна повторяться ТА ЖЕ модель (лестница)"
        if max_tokens == 1200:
            return _completion("", "length")
        return _completion('{"overview": "ок"}', "stop")

    calls: list[tuple[str, int]] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        max_tokens = json["max_tokens"]
        calls.append((model, max_tokens))
        return httpx.Response(
            200, json=responder(model, max_tokens), request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await client_high_cap.generate("p", max_tokens=1200)
    assert result == '{"overview": "ок"}'
    assert calls == [("chain/model-1", 1200), ("chain/model-1", 2400)]


async def test_all_models_length_truncated_but_empty_is_not_last_resort(client, monkeypatch):
    """Обе модели finish_reason=length с пустым content даже на потолке —
    ни один пустой truncated-текст не годится в last resort: должен
    получиться FREE_CHAIN_EXHAUSTED, а не успех с пустой строкой."""
    _empty_catalog(monkeypatch)
    calls = _wire_responses(
        monkeypatch,
        {
            "chain/model-1": _completion("", "length"),
            "chain/model-2": _completion("", "length"),
        },
    )
    with pytest.raises(RuntimeError) as exc_info:
        await client.generate("p", max_tokens=1200)

    from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER

    assert FREE_CHAIN_EXHAUSTED_MARKER in str(exc_info.value)
    assert calls  # хоть что-то было вызвано


# ── paid-путь: одиночная модель, пустой ответ должен ретраиться, а не
# немедленно возвращаться как успех ──────────────────────────────────────


async def test_paid_path_empty_response_retries_then_fails(client, monkeypatch):
    client.set_paid_mode(True)
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        calls.append(json["model"])
        return httpx.Response(
            200, json=_completion("", "stop"), request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.llm_client.asyncio.sleep", lambda *a, **k: _noop())

    with pytest.raises(RuntimeError):
        await client.generate("p")
    # LLM_GENERATE_MAX_ATTEMPTS попыток на единственную модель.
    from app.llm_client import LLM_GENERATE_MAX_ATTEMPTS

    assert calls == ["chain/paid-model"] * LLM_GENERATE_MAX_ATTEMPTS


async def _noop():
    return None


async def test_paid_path_length_empty_retries_then_fails_not_empty_success(client, monkeypatch):
    """Q8 ревью-фикс (Important 2): единственная платная модель отвечает
    finish_reason=length с ПУСТЫМ content на каждой попытке. exc.text пуст —
    _generate_with_retries не должен вернуть его как «успех» (в отличие от
    непустого truncated.text, который отдаётся немедленно как last resort
    для одиночной модели без цепочки) — должен ретраить как обычную
    retriable-ошибку и в итоге упасть с RuntimeError."""
    client.set_paid_mode(True)
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        calls.append(json["model"])
        return httpx.Response(
            200, json=_completion("", "length"), request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.llm_client.asyncio.sleep", lambda *a, **k: _noop())

    with pytest.raises(RuntimeError):
        await client.generate("p")

    from app.llm_client import LLM_GENERATE_MAX_ATTEMPTS

    assert calls == ["chain/paid-model"] * LLM_GENERATE_MAX_ATTEMPTS


# ── Ревью-фикс (Important 1): short-circuit vs last-resort — два разных
# понятия, раньше слитых в одно условие. Обрезка (truncated), пустая или не —
# ВСЕГДА truncation-класс для short-circuit (следующий проход даст тот же
# потолок, тот же брак); last-resort кандидатом становится ТОЛЬКО непустой
# text. «Все truncated и все пустые» — короткий один проход, ноль sleep'ов,
# итог — FREE_CHAIN_EXHAUSTED (кандидата на last resort нет), а НЕ 3 полных
# прохода с sleep'ами между ними (то, что раньше воспроизводил ревьюер на
# прод-конфиге: pass_had_real_failure ошибочно становился True для пустого
# truncated, и truncated_text is not None никогда не выполнялось → внешний
# short-circuit не срабатывал вовсе).


@pytest.fixture
def client_prod_passes(tmp_path, monkeypatch):
    """Прод-конфиг: 5 моделей, OPENROUTER_FALLBACK_RETRY_PASSES=2 (passes=3) —
    как ``client_prod_passes`` в tests/test_adaptive_max_tokens.py."""
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


async def test_all_models_length_empty_prod_config_stops_after_one_pass_exhausted(
    client_prod_passes, monkeypatch
):
    """RED-репро ревью Important 1: все 5 моделей отвечают finish_reason=length
    с ПУСТЫМ content на каждой ступени лестницы (2000→4000→8000). Обрезка —
    truncation-класс независимо от пустоты: должен получиться РОВНО один
    проход (никакого sleep между проходами), а исход — FREE_CHAIN_EXHAUSTED
    (нет непустого last-resort кандидата), а не успех с пустой строкой и не
    3 полных прохода."""
    calls: list[str] = []
    sleeps: list[float] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        return httpx.Response(
            200,
            json=_completion("", "length"),
            request=httpx.Request("POST", url),
        )

    async def empty_catalog(self, url, headers=None):
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", empty_catalog)
    monkeypatch.setattr("app.llm_client.asyncio.sleep", fake_sleep)

    from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER

    with pytest.raises(RuntimeError) as exc_info:
        await client_prod_passes.generate("p", max_tokens=2000)

    assert FREE_CHAIN_EXHAUSTED_MARKER in str(exc_info.value)
    # 5 моделей × 3 запроса (лестница 2000→4000→8000, cap=8000) — РОВНО один
    # проход (15 запросов), не 3 прохода (45 запросов).
    assert len(calls) == 15, calls
    # Ни одного sleep между проходами — short-circuit сработал на 1-м проходе.
    assert sleeps == []
