"""Динамический хвост free-цепочки: отбор моделей из каталога OpenRouter.

Спека: docs/superpowers/specs/2026-07-14-dynamic-free-chain-tail-design.md
"""

import httpx
import pytest

from app.config import load_settings
from app.db import Database
from app.llm_client import OpenRouterClient, _select_dynamic_tail


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


async def test_catalog_cached_within_ttl(client, monkeypatch):
    counter = [0]
    _wire_catalog(monkeypatch, [_entry("x/model:free", 200000)], counter)
    first = await client._catalog_models()
    second = await client._catalog_models()
    assert counter[0] == 1
    assert first == second == [{"id": "x/model:free", "context_length": 200000}]
