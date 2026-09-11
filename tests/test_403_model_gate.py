"""HTTP 403 от OpenRouter → trying_next (боевой инцидент 2026-09-11).

OpenRouter отвечает 403 на МОДЕЛЬ-специфичные отказы: гейт free-модели
("thinkingmachines/inkling-small:free is only available on agentic
harnesses") или модерацию входа для конкретной модели. Это не «плохой
API-ключ» (тот — 401): следующая модель в цепочке может ответить нормально.
До фикса 403 был non-retriable и ронял всю job с техническим сообщением
пользователю; теперь — переход к следующей модели, а полный отказ цепочки
уходит в FREE_CHAIN_EXHAUSTED, который pipeline (Q8) перезапускает сам.
"""

import httpx
import pytest

from app.config import load_settings
from app.db import Database
from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER, OpenRouterClient


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


def _completion(content: str, finish_reason: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


# Дословное тело боевого инцидента (0fs7fDaNd3c, 2026-09-11).
_AGENTIC_GATE_BODY = {
    "error": {
        "message": (
            "thinkingmachines/inkling-small:free is only available on agentic "
            "harnesses. Try plugging it into a coding agent or productivity app "
            "listed on https://openrouter.ai/apps"
        ),
        "code": 403,
        "metadata": {"failed_routing_step": "Gate Free Endpoints by Agentic Harness"},
    }
}


async def test_403_model_gate_falls_through_to_next_model(client, monkeypatch):
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        if model == "chain/model-1":
            return httpx.Response(
                403, json=_AGENTIC_GATE_BODY, request=httpx.Request("POST", url)
            )
        return httpx.Response(
            200,
            json=_completion('{"overview": "ок"}', "stop"),
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await client.generate("p")

    assert result == '{"overview": "ок"}'
    assert calls == ["chain/model-1", "chain/model-2"]


async def test_all_models_403_becomes_free_chain_exhausted(client, monkeypatch):
    """Все модели за гейтом → не голый «HTTP 403» пользователю, а маркер
    исчерпания цепочки, который Q8-механика pipeline перезапускает сама."""
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        calls.append(json["model"])
        return httpx.Response(
            403, json=_AGENTIC_GATE_BODY, request=httpx.Request("POST", url)
        )

    async def no_catalog():
        raise RuntimeError("catalog down in test")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(client, "_catalog_models", no_catalog)

    with pytest.raises(RuntimeError) as exc_info:
        await client.generate("p")

    message = str(exc_info.value)
    assert FREE_CHAIN_EXHAUSTED_MARKER in message
    assert "403" in message
    # Обе модели попробованы — 403 больше не рвёт цепочку на первой.
    assert calls == ["chain/model-1", "chain/model-2"]


async def test_401_bad_key_stays_non_retriable(client, monkeypatch):
    """401 (невалидный ключ) — по-прежнему фатален для всей цепочки: повтор
    с тем же ключом бессмысленен на любой модели."""
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        calls.append(json["model"])
        return httpx.Response(
            401,
            json={"error": {"message": "No auth credentials found", "code": 401}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(RuntimeError) as exc_info:
        await client.generate("p")

    assert "401" in str(exc_info.value)
    assert calls == ["chain/model-1"]
