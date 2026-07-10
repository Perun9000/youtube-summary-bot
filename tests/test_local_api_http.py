from aiohttp.test_utils import TestClient, TestServer

from app.local_api import create_local_api_app, start_local_api


TOKEN = "s3cr3t-token"
VIDEO_ID = "dQw4w9WgXcQ"


class _FakeSettings:
    def __init__(self, local_api_token=TOKEN, local_api_port=8799, owner_user_id=5779821):
        self.local_api_token = local_api_token
        self.local_api_port = local_api_port
        self.owner_user_id = owner_user_id


class _FakeServices:
    def __init__(self, **settings_kwargs):
        self.settings = _FakeSettings(**settings_kwargs)


async def _make_client(services):
    app = create_local_api_app(services)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


async def test_enqueue_requires_token():
    services = _FakeServices()
    client = await _make_client(services)
    try:
        resp = await client.post("/enqueue", json={"video_id": VIDEO_ID})
        assert resp.status == 401
        body = await resp.json()
        assert body == {"error": "unauthorized"}
    finally:
        await client.close()


async def test_enqueue_rejects_wrong_token():
    services = _FakeServices()
    client = await _make_client(services)
    try:
        resp = await client.post(
            "/enqueue",
            json={"video_id": VIDEO_ID},
            headers={"X-Auth-Token": "wrong-token"},
        )
        assert resp.status == 401
        body = await resp.json()
        assert body == {"error": "unauthorized"}
    finally:
        await client.close()


async def test_enqueue_rejects_bad_video_id():
    services = _FakeServices()
    client = await _make_client(services)
    try:
        resp = await client.post(
            "/enqueue",
            json={"video_id": "short"},
            headers={"X-Auth-Token": TOKEN},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body == {"error": "bad_video_id"}
    finally:
        await client.close()


async def test_enqueue_queued(monkeypatch):
    import app.local_api as local_api

    async def fake_enqueue(video_id, services):
        assert video_id == VIDEO_ID
        return "queued"

    monkeypatch.setattr(local_api, "enqueue_local_api_job", fake_enqueue)

    services = _FakeServices()
    client = await _make_client(services)
    try:
        resp = await client.post(
            "/enqueue",
            json={"video_id": VIDEO_ID},
            headers={"X-Auth-Token": TOKEN},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"status": "queued"}
    finally:
        await client.close()


async def test_enqueue_cached(monkeypatch):
    import app.local_api as local_api

    async def fake_enqueue(video_id, services):
        return "cached"

    monkeypatch.setattr(local_api, "enqueue_local_api_job", fake_enqueue)

    services = _FakeServices()
    client = await _make_client(services)
    try:
        resp = await client.post(
            "/enqueue",
            json={"video_id": VIDEO_ID},
            headers={"X-Auth-Token": TOKEN},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"status": "cached"}
    finally:
        await client.close()


async def test_enqueue_internal_error(monkeypatch):
    import app.local_api as local_api

    async def fake_enqueue(video_id, services):
        raise RuntimeError("boom")

    monkeypatch.setattr(local_api, "enqueue_local_api_job", fake_enqueue)

    services = _FakeServices()
    client = await _make_client(services)
    try:
        resp = await client.post(
            "/enqueue",
            json={"video_id": VIDEO_ID},
            headers={"X-Auth-Token": TOKEN},
        )
        assert resp.status == 500
        body = await resp.json()
        assert body == {"error": "internal"}
    finally:
        await client.close()


async def test_options_preflight_cors():
    services = _FakeServices()
    client = await _make_client(services)
    try:
        resp = await client.options("/enqueue")
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "https://www.youtube.com"
        assert "X-Auth-Token" in resp.headers["Access-Control-Allow-Headers"]
        assert resp.headers["Access-Control-Allow-Private-Network"] == "true"
    finally:
        await client.close()


async def test_start_disabled_without_token():
    services = _FakeServices(local_api_token="")
    runner = await start_local_api(services)
    assert runner is None


async def test_start_disabled_without_owner():
    services = _FakeServices(owner_user_id=None)
    runner = await start_local_api(services)
    assert runner is None
