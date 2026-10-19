# -*- coding: utf-8 -*-
"""#448 backend tests: /v1/models discovery + the timeout-vs-unreachable
distinction in synthesize_remote."""
from __future__ import annotations

import httpx
import pytest

from backend.agent import speak

_RealAsyncClient = httpx.AsyncClient

MOSS_MODELS = {
    "object": "list",
    "data": [{"id": "moss-tts-realtime", "object": "model", "owned_by": "herp"}],
}


def _install(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        speak.httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=transport)
    )


async def test_probe_models_parses_openai_list_shape(monkeypatch):
    """MOSS box shape: GET {base}/v1/models -> {"object":"list","data":
    [{"id": ...}]} -> the ids, deduplicated, order preserved."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=MOSS_MODELS)

    _install(monkeypatch, handler)
    models = await speak.probe_remote_models("http://moss.local:8086", api_key="sk-x")
    assert models == ["moss-tts-realtime"]
    assert seen["url"] == "http://moss.local:8086/v1/models"
    assert seen["auth"] == "Bearer sk-x"


async def test_probe_models_accepts_models_key_and_v1_base(monkeypatch):
    """{"models": [...]} is tolerated, and a /v1-suffixed base does not
    double the suffix."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://box:8081/v1/models"
        return httpx.Response(200, json={"models": ["a", "b", "a"]})

    _install(monkeypatch, handler)
    assert await speak.probe_remote_models("http://box:8081/v1") == ["a", "b"]


async def test_probe_models_failure_is_empty_never_error(monkeypatch):
    """Same posture as voices: 404, 500, bad JSON, timeout, refused -> []."""

    def not_found(req):
        return httpx.Response(404, json={"detail": "Not Found"})

    def server_error(req):
        return httpx.Response(500)

    def bad_json(req):
        return httpx.Response(200, content=b"<html>not json</html>")

    def timeout(req):
        raise httpx.ConnectTimeout("timed out")

    def refused(req):
        raise httpx.ConnectError("refused")

    for handler in (not_found, server_error, bad_json, timeout, refused):
        _install(monkeypatch, handler)
        assert await speak.probe_remote_models("http://box:8081") == [], handler.__name__


async def test_normalize_models_accepts_bare_array_dialect(monkeypatch):
    """#448's spec asks for bare arrays too: ids as plain strings or as
    {"id": ...} objects; junk dropped, trimmed, deduplicated, order kept."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=["af_heart", "af_heart", " af_nicole ", {"id": "moss-tts-realtime"}, 5, None],
        )

    _install(monkeypatch, handler)
    assert await speak.probe_remote_models("http://box:8081") == [
        "af_heart",
        "af_nicole",
        "moss-tts-realtime",
    ]
    # And the module-level helper agrees, for non-route callers.
    assert speak._normalize_models(["af_heart", "", {"id": "m"}, {"id": 3}]) == [
        "af_heart",
        "m",
    ]
    assert speak._normalize_models({"data": [{"id": "m"}, "junk", {"id": ""}, {"id": "m2"}]}) == [
        "m",
        "m2",
    ]


async def test_timeout_error_says_timed_out_not_connection_failed(monkeypatch):
    """#448's user-facing point: a server that is merely SLOW (CPU-served
    MOSS takes minutes) must not read as 'connection failed'. The timeout
    message names the budget; the error carries timed_out=True."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    _install(monkeypatch, handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            "Voice test.",
            endpoint="http://moss.local:8086",
            api_key="",
            model="m",
            voice="v",
            speed=None,
        )
    assert "timed out after 180s" in str(ei.value)
    assert "connection failed" not in str(ei.value)
    assert ei.value.timed_out is True


async def test_transport_error_still_says_connection_failed(monkeypatch):
    """A refused/unreachable server keeps the connection-failed wording and
    timed_out stays False."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("active refused")

    _install(monkeypatch, handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            "x", endpoint="http://dead.local:1", api_key="", model="m", voice="v", speed=None
        )
    assert "connection failed" in str(ei.value)
    assert ei.value.timed_out is False


@pytest.mark.asyncio
async def test_connect_timeout_reads_as_connection_failed(monkeypatch):
    """Review round (#449): ConnectTimeout means NO connection was ever
    made - a dead host must fail fast as 'connection failed' (and
    timed_out False), not wear the 'still synthesizing' wording after a
    three-minute hang."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    _install(monkeypatch, handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            "x", endpoint="http://dead.local:1", api_key="", model="m", voice="v", speed=None
        )
    assert "connection failed" in str(ei.value)
    assert "timed out after" not in str(ei.value)
    assert ei.value.timed_out is False


def test_connect_phase_has_its_own_short_budget():
    """The 180 s budget belongs to the read (synthesis) phase only; the
    TCP handshake gets its own short REMOTE_CONNECT_TIMEOUT."""
    t = httpx.Timeout(speak.REMOTE_TIMEOUT, connect=speak.REMOTE_CONNECT_TIMEOUT)
    assert t.connect == 5.0
    assert t.read == 180.0
