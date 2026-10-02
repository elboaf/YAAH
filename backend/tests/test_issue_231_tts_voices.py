"""#231: remote TTS voice discovery (GET /voices probe).

The reference Kokoro server (herp.local) exposes a non-standard
``GET /voices`` returning a bare JSON array of voice names; OpenAI's own
API has no voices endpoint at all. Discovery is therefore best-effort by
definition: a list-less or unreachable server must read as "no list"
(free-text fallback), never as an error. Verified against a fake
transport, never the network (the #205 pattern).
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from backend.agent import config as config_mod
from backend.agent import speak
from backend.main import app


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_PATH to a per-test file: config PUTs here never leak
    into other modules, and each test starts from DEFAULTS (the #140
    pattern, reused by the #205 tests)."""
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")


# The REAL client class, captured before any test patches speak.httpx.
_RealAsyncClient = httpx.AsyncClient


class _RecordingTransport(httpx.MockTransport):
    """Captures each request; handler returns the canned response."""

    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.handler(request)


def _client(transport) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=transport)


# ---- 1. the probe (speak.probe_remote_voices) ----


async def test_probe_parses_bare_string_array(monkeypatch):
    """herp.local's shape: GET {base}/voices -> 200, a bare JSON array of
    voice names, returned in server order."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=["af_alloy", "af_heart", "zm_yunyang"])

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))

    voices = await speak.probe_remote_voices("http://herp.local:8081", api_key="sk-test")

    assert seen["url"] == "http://herp.local:8081/voices"
    assert seen["auth"] == "Bearer sk-test"  # the key goes ONLY to the probe target
    assert voices == ["af_alloy", "af_heart", "zm_yunyang"]


async def test_probe_strips_v1_suffix(monkeypatch):
    """Kokoro-family servers mount /voices at the ROOT even when the speech
    route lives under /v1 (verified: /v1/voices is 404 on herp.local). A
    base ending in /v1 still probes the server root."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=["af_heart"])

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    await speak.probe_remote_voices("http://box:8081/v1", api_key="")
    assert seen == ["http://box:8081/voices"]


async def test_probe_tolerates_alternate_shapes(monkeypatch):
    """Other plausible list shapes normalize: {"voices": [str]},
    {"data": [{"id": str}]} (OpenAI-list style), a name-keyed map.
    Non-string junk inside a list is dropped; duplicates collapse."""
    cases = [
        ({"voices": ["af_heart", "af_nicole"]}, ["af_heart", "af_nicole"]),
        ({"data": [{"id": "alloy"}, {"id": "echo"}]}, ["alloy", "echo"]),
        ({"af_heart": {"lang": "en-us"}, "af_nicole": {}}, ["af_heart", "af_nicole"]),
        (["af_heart", 3, None, "", "af_heart", {"id": "vm_x"}], ["af_heart", "vm_x"]),
    ]
    for body, expected in cases:
        transport = _RecordingTransport(lambda req, b=body: httpx.Response(200, json=b))
        monkeypatch.setattr(
            speak.httpx, "AsyncClient", lambda t=transport, **kw: _client(t)
        )
        assert await speak.probe_remote_voices("http://box:8081", api_key="") == expected


async def test_probe_failures_yield_empty_list(monkeypatch):
    """A list-less world is NORMAL (OpenAI has no voices endpoint): any
    non-200, timeout, unreachable server, or unparseable body reads as
    "no list" — [] — never a raised error."""

    def not_found(req):
        return httpx.Response(404, json={"detail": "Not Found"})

    def server_error(req):
        return httpx.Response(500, text="boom")

    def bad_json(req):
        return httpx.Response(200, content=b"<html>not json</html>")

    def wrong_type(req):
        return httpx.Response(200, json={"ok": True})  # no list in sight

    def timeout(req):
        raise httpx.ConnectTimeout("timed out")

    def transport_error(req):
        raise httpx.ConnectError("refused")

    for handler in (not_found, server_error, bad_json, wrong_type, timeout, transport_error):
        transport = _RecordingTransport(handler)
        monkeypatch.setattr(
            speak.httpx, "AsyncClient", lambda t=transport, **kw: _client(t)
        )
        assert await speak.probe_remote_voices("http://box:8081", api_key="") == [], handler.__name__


async def test_probe_no_auth_header_when_no_key(monkeypatch):
    """No Authorization header when no key is configured (the #205
    synthesize posture)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=["af_heart"])

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    await speak.probe_remote_voices("http://box:8081", api_key="")
    assert seen["auth"] is None


# ---- 2. the route (POST /api/tts/voices) ----


async def test_route_returns_voice_list(monkeypatch):
    """POST /api/tts/voices {endpoint} -> 200 {"voices": [...]}."""
    transport = _RecordingTransport(lambda req: httpx.Response(200, json=["af_heart", "af_nicole"]))
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/voices", json={"endpoint": "http://box:8081"})
    assert res.status_code == 200
    assert res.json() == {"voices": ["af_heart", "af_nicole"]}


async def test_route_409_without_any_endpoint():
    """No draft endpoint and none stored -> 409 not-configured (the shared
    code shape the Test button uses)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/voices", json={})
    assert res.status_code == 409
    assert res.json()["code"] == "not-configured"


async def test_route_draft_key_overrides_stored_key(monkeypatch):
    """A typed-but-unsaved key draft must reach the probe (first-time setup:
    discovery runs BEFORE the user hits Save, so the stored key is empty)."""
    from backend.agent.config import save_config

    save_config({"voice": {"tts_api_key": "sk-stored"}})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization") or "")
        return httpx.Response(200, json=["af_heart"])

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        draft = await client.post(
            "/api/tts/voices", json={"endpoint": "http://box:8081", "api_key": "sk-draft"}
        )
        saved = await client.post("/api/tts/voices", json={"endpoint": "http://box:8081"})

    assert draft.status_code == saved.status_code == 200
    # Draft first, stored fallback second — in that order, one probe each.
    assert seen == ["Bearer sk-draft", "Bearer sk-stored"]


async def test_route_empty_list_when_server_has_no_route(monkeypatch):
    """A 404 server still answers 200 {"voices": []} — the UI's cue to show
    free-text, not an error banner."""
    transport = _RecordingTransport(lambda req: httpx.Response(404, json={"detail": "Not Found"}))
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/voices", json={"endpoint": "http://box:8081"})
    assert res.status_code == 200
    assert res.json() == {"voices": []}


async def test_route_falls_back_to_stored_endpoint(monkeypatch):
    """No draft endpoint but one saved: probe the saved one (Settings on
    first paint probes before the user touches the field)."""
    from backend.agent.config import save_config

    save_config({"voice": {"tts_endpoint": "http://saved:8081"}})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=["af_heart"])

    transport = _RecordingTransport(handler)
    # Route resolves speak lazily; patch after import inside app too.
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/tts/voices", json={})
    assert res.status_code == 200
    assert seen == ["http://saved:8081/voices"]
