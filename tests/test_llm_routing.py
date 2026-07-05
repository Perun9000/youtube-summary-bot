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
