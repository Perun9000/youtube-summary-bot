import httpx
import pytest

from app.config import Settings
from app.telegraph_service import TelegraphService


def make_service(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    from app.config import load_settings
    svc = TelegraphService(load_settings())
    svc._access_token = "token"
    return svc


async def test_retries_then_succeeds(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def fake_post(self, url, data=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True, "result": {"url": "https://telegra.ph/ok"}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.telegraph_service.RETRY_DELAYS_SEC", (0, 0))
    result = await svc._post_with_retries("createPage", {"title": "t"})
    assert result["result"]["url"] == "https://telegra.ph/ok"
    assert calls["n"] == 3


async def test_gives_up_after_attempts(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)

    async def fake_post(self, url, data=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.telegraph_service.RETRY_DELAYS_SEC", (0, 0))
    with pytest.raises(httpx.ConnectError):
        await svc._post_with_retries("createPage", {"title": "t"})


async def test_4xx_is_not_retried(tmp_path, monkeypatch):
    """4xx are our own data errors (e.g. PAGE_ACCESS_DENIED) — retrying
    won't change the outcome, so _post_with_retries must fail fast."""
    svc = make_service(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def fake_post(self, url, data=None):
        calls["n"] += 1
        return httpx.Response(
            400, json={"ok": False, "error": "BAD_REQUEST"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.telegraph_service.RETRY_DELAYS_SEC", (0, 0))
    with pytest.raises(httpx.HTTPStatusError):
        await svc._post_with_retries("createPage", {"title": "t"})
    assert calls["n"] == 1
