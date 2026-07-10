"""Обрыв ответа по max_tokens (finish_reason=length).

Reasoning-модели (nemotron и т.п.) могут зациклиться и выжечь весь лимит
completion-токенов, так и не дойдя до JSON. Такой ответ — брак: free-цепочка
должна попробовать следующую модель, а обрезанный текст сохранить только как
last resort, когда вся цепочка исчерпана.
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
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_PASSES", "0")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


def _wire_responses(monkeypatch, responses_by_model: dict[str, dict]):
    """Подменить httpx-POST: ответ выбирается по payload['model']."""
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        body = responses_by_model[model]
        return httpx.Response(
            200, json=body, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def _completion(content: str, finish_reason: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


async def test_truncated_response_falls_through_to_next_model(client, monkeypatch):
    calls = _wire_responses(
        monkeypatch,
        {
            "chain/model-1": _completion("We need to produce JSON with overview", "length"),
            "chain/model-2": _completion('{"overview": "ок"}', "stop"),
        },
    )
    usage = GenerationUsage()
    result = await client.generate("p", usage=usage)
    assert result == '{"overview": "ок"}'
    assert calls == ["chain/model-1", "chain/model-2"]
    assert usage.last_finish_reason == "stop"


async def test_all_models_truncated_returns_last_resort_text(client, monkeypatch):
    calls = _wire_responses(
        monkeypatch,
        {
            "chain/model-1": _completion("loop loop loop", "length"),
            "chain/model-2": _completion("another loop", "length"),
        },
    )
    usage = GenerationUsage()
    result = await client.generate("p", usage=usage)
    # Обе модели упёрлись в лимит — отдаём последний обрезанный текст,
    # а не роняем цепочку: downstream-парсер сам решит, что с ним делать.
    assert result == "another loop"
    assert calls == ["chain/model-1", "chain/model-2"]
    assert usage.last_finish_reason == "length"


async def test_normal_response_keeps_finish_reason_stop(client, monkeypatch):
    _wire_responses(
        monkeypatch,
        {"chain/model-1": _completion('{"overview": "ок"}', "stop")},
    )
    usage = GenerationUsage()
    result = await client.generate("p", usage=usage)
    assert result == '{"overview": "ок"}'
    assert usage.last_finish_reason == "stop"
