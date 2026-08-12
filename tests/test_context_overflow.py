"""Ревью-фикс Q7 (Important 2): HTTP 400 context-length-exceeded → trying_next.

OpenRouter (и большинство OpenAI-совместимых провайдеров) отвечает HTTP 400
и для "запрос сломан" (non-retriable — повтор бессмысленен), и для
"prompt+max_tokens не влезает в контекст ЭТОЙ модели" (context-length-
exceeded — свойство конкретной модели, следующая в цепочке может иметь
контекст побольше). Без различения второй случай ронял бы всю job
(RuntimeError мимо ``_OpenRouterRetriable``) вместо перехода на следующую
модель. Сегодняшняя free-цепочка безопасна (все модели 256K-1M контекста),
но инвариант — "каждая модель в цепочке имеет контекст >= prompt + 8000" —
не проверяется в рантайме (см. комментарий у ``Settings.llm_big_prompt_chars``
в app/config.py), так что классификация — единственная защита от будущей
малоконтекстной модели в фиксированной цепочке.
"""

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


def _completion(content: str, finish_reason: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


async def test_400_context_length_exceeded_falls_through_to_next_model(client, monkeypatch):
    """Реальный формат OpenAI-совместимых провайдеров: "This model's maximum
    context length is 8192 tokens...". Модель-1 (маленький контекст) не
    должна ронять job — цепочка переходит на модель-2."""
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        if model == "chain/model-1":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "This model's maximum context length is 8192 tokens. "
                            "However, your messages resulted in 91234 tokens."
                        ),
                        "code": 400,
                    }
                },
                request=httpx.Request("POST", url),
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


async def test_400_context_length_exceeded_code_style_falls_through(client, monkeypatch):
    """Второй распространённый формат: {"error": {"code": "context_length_exceeded"}}."""
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        if model == "chain/model-1":
            return httpx.Response(
                400,
                json={"error": {"message": "context_length_exceeded", "code": 400}},
                request=httpx.Request("POST", url),
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


async def test_400_other_body_stays_non_retriable(client, monkeypatch):
    """400 без context-overflow маркеров — семантика "4xx не ретраим"
    сохраняется: job падает целиком, следующая модель НЕ вызывается."""
    calls: list[str] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid request: 'messages' is required.", "code": 400}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(RuntimeError) as exc_info:
        await client.generate("p")

    assert "context" not in str(exc_info.value).lower()
    # Non-retriable — цепочка не пробует следующую модель.
    assert calls == ["chain/model-1"]
