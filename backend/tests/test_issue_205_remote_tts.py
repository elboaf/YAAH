"""#205: remote TTS engine (OpenAI-compatible /v1/audio/speech).

The remote path is a STANDARD OpenAI speech client: only
model/input/voice (+speed when the user set one), response_format=wav,
raw audio bytes on 200. Verified against a fake transport, never the
network. The dictation precedent (transcribe.transcribe_cloud) is the
config shape; the reference server is a faithful compatible subset, so
anything that works here works on api.openai.com too.
"""

from __future__ import annotations

import io
import json
import wave

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from backend.agent import config as config_mod
from backend.agent import speak
from backend.main import app


@pytest.fixture
def tts_env(tmp_path, monkeypatch):
    """Fake local model dir: the resolver only checks for the file set."""
    d = tmp_path / "kokoro-multi-lang-v1_0"
    d.mkdir()
    for f in ("model.onnx", "voices.bin", "tokens.txt"):
        (d / f).write_text("stub")
    (d / "espeak-ng-data").mkdir()
    monkeypatch.setenv("YAAH_TTS_MODEL_DIR", str(d))
    return d


def _wav_bytes(pcm: bytes = b"\x00\x01" * 2400, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---- 1. the standard-client POST contract (speak.synthesize_remote) ----


@pytest.fixture(autouse=True)
def clean_floor():
    """Reset the module-level supersede floor around every test (#317):
    these tests post low epochs (1..3) against the real floor, so a floor
    raised by any earlier test in the same pytest process turned
    200/502 into 409 (suite order failed, isolation passed). Reset on
    both ends so this module never leaks either."""
    speak._floor = 0
    yield
    speak._floor = 0


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_PATH to a per-test file: config PUTs here never leak
    into other modules (test_speak.py narrates with local defaults), and
    each test starts from DEFAULTS (the #140 pattern)."""
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")


# The REAL client class, captured before any test patches speak.httpx.
_RealAsyncClient = httpx.AsyncClient


class _RecordingTransport(httpx.MockTransport):
    """Captures each request; handler returns per-attempt responses.
    (The async path bypasses handle_request — MockTransport hands the
    handler the request directly — so override handle_async_request.)"""

    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.handler(request)


def _client(transport) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=transport)


async def test_remote_posts_openai_contract(monkeypatch):
    """A standard OpenAI speech request: POST {base}/v1/audio/speech with
    model/input/voice/response_format=wav; no speed unless the user set
    one; no instructions, no stream_format. Raw bytes come back."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, content=_wav_bytes(b"\x01\x02" * 100))

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))

    expected = _wav_bytes(b"\x01\x02" * 100)
    data = await speak.synthesize_remote(
        "Hello there.",
        endpoint="http://herp.local:8081",
        api_key="sk-test",
        model="kokoro",
        voice="af_heart",
        speed=None,
    )

    assert seen["url"] == "http://herp.local:8081/v1/audio/speech"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"] == {
        "model": "kokoro",
        "input": "Hello there.",
        "voice": "af_heart",
        "response_format": "wav",
    }
    assert data == expected  # byte-for-byte passthrough of the server's WAV


async def test_remote_appends_suffix_only_once(monkeypatch):
    """Accept a base that already ends in /v1 (or the full suffix path)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=_wav_bytes())

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    for base in (
        "http://box:8081",
        "http://box:8081/",
        "http://box:8081/v1",
        "http://box:8081/v1/",
        "http://box:8081/v1/audio/speech",
    ):
        await speak.synthesize_remote(
            "x", endpoint=base, api_key="", model="kokoro", voice="af_heart", speed=None
        )
    assert seen == ["http://box:8081/v1/audio/speech"] * 5


async def test_remote_omits_key_header_when_blank(monkeypatch):
    """No Authorization header when no key is configured."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, content=_wav_bytes())

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    await speak.synthesize_remote(
        "x", endpoint="http://box:8081", api_key="", model="m", voice="v", speed=None
    )


async def test_remote_speed_sent_only_when_set(monkeypatch):
    """speed is user-optional; when set it rides along untouched (the
    server validates its own range)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body["input"] == "set":
            assert body["speed"] == 0.8
        else:
            assert "speed" not in body
        return httpx.Response(200, content=_wav_bytes())

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    await speak.synthesize_remote(
        "unset", endpoint="http://box", api_key="", model="m", voice="v", speed=None
    )
    await speak.synthesize_remote(
        "set", endpoint="http://box", api_key="", model="m", voice="v", speed=0.8
    )


# ---- 2. errors: tolerant parsing, timeout -> retry -> skip ----


async def test_remote_4xx_openai_error_shape(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "Incorrect API key", "type": "invalid_request_error"}},
        )

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            "x", endpoint="http://box", api_key="bad", model="m", voice="v", speed=None
        )
    assert "Incorrect API key" in str(ei.value)
    assert len(transport.requests) == 1  # a 4xx is not retried


async def test_remote_4xx_kokoro_error_shape(monkeypatch):
    """The reference server answers with FastAPI's {"detail": ...} shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "Model not found"})

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            "x", endpoint="http://box", api_key="", model="m", voice="v", speed=None
        )
    assert "Model not found" in str(ei.value)


async def test_remote_error_non_json_body_surfaces_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            "x", endpoint="http://box", api_key="", model="m", voice="v", speed=None
        )
    assert "502" in str(ei.value)


async def test_remote_timeout_retries_then_raises(monkeypatch):
    """One retry after a timeout (so one lost packet doesn't drop a
    sentence), then the error carries enough detail to surface in
    Settings."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("timed out")

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            "x", endpoint="http://box", api_key="", model="m", voice="v", speed=None
        )
    assert calls["n"] == 2
    assert "timed out" in str(ei.value).lower() or "timeout" in str(ei.value).lower()


async def test_remote_5xx_retries_then_surfaces(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"detail": "model loading"})

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_remote(
            "x", endpoint="http://box", api_key="", model="m", voice="v", speed=None
        )
    assert calls["n"] == 2
    assert "model loading" in str(ei.value)


async def test_remote_retry_then_success(monkeypatch):
    """The retry path ends in audio, not an error."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow first try")
        return httpx.Response(200, content=_wav_bytes())

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    data = await speak.synthesize_remote(
        "x", endpoint="http://box", api_key="", model="m", voice="v", speed=None
    )
    assert calls["n"] == 2


async def test_remote_empty_body_is_error(monkeypatch):
    """A 200 with zero bytes is a broken server, not silence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    transport = _RecordingTransport(handler)
    monkeypatch.setattr(speak.httpx, "AsyncClient", lambda **kw: _client(transport))
    with pytest.raises(speak.RemoteTTSError):
        await speak.synthesize_remote(
            "x", endpoint="http://box", api_key="", model="m", voice="v", speed=None
        )


# ---- 3. config: defaults, round-trip, masked key ----


def test_voice_defaults_include_tts_keys(monkeypatch):
    """The five new keys exist with the spec's defaults; local defaults are
    untouched, so existing installs narrate exactly as before."""
    d = config_mod.DEFAULTS["voice"]
    assert d["tts_engine"] == "local"
    assert d["tts_endpoint"] == ""
    assert d["tts_api_key"] == ""
    assert d["tts_model"] == "kokoro"
    assert d["tts_voice"] == "af_heart"
    assert d["tts_speed"] is None


async def test_config_roundtrip_masks_tts_api_key():
    """tts_api_key is masked like cloud_api_key: GET shows 'set', PUT
    'set'/'' is dropped so a Settings save never wipes the stored key."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local:8081",
                    "tts_api_key": "sk-secret-123",
                    "tts_model": "kokoro",
                    "tts_voice": "af_nicole",
                    "tts_speed": 1.1,
                }
            },
        )
        assert res.status_code == 200
        got = (await client.get("/api/config")).json()
    v = got["voice"]
    assert v["tts_engine"] == "remote"
    assert v["tts_endpoint"] == "http://herp.local:8081"
    assert v["tts_api_key"] == "set"  # masked, never the key itself
    assert v["tts_model"] == "kokoro"
    assert v["tts_voice"] == "af_nicole"
    assert v["tts_speed"] == 1.1

    # A settings round-trip that resends the mask keeps the stored key.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={"voice": {"tts_engine": "remote", "tts_api_key": "set"}},
        )
        from backend.agent.config import load_config

        stored = load_config()["voice"]
    assert stored["tts_api_key"] == "sk-secret-123"


# ---- 4. /api/tts/status reflects the active engine ----


@pytest.mark.asyncio
async def test_status_local_engine(tts_env, monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put("/api/config", json={"voice": {"tts_engine": "local"}})
        body = (await client.get("/api/tts/status")).json()
    assert body["engine"] == "local"
    assert body["available"] is True  # local model present


@pytest.mark.asyncio
async def test_status_remote_engine_ready(tts_env, monkeypatch):
    """Remote + configured endpoint = available even with NO local model."""

    def no_model():
        return None

    monkeypatch.setattr(speak, "find_model_dir", no_model)
    monkeypatch.setattr(speak, "model_available", lambda: False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={"voice": {"tts_engine": "remote", "tts_endpoint": "http://herp.local:8081"}},
        )
        body = (await client.get("/api/tts/status")).json()
    assert body["engine"] == "remote"
    assert body["available"] is True
    assert body["model_available"] is False


@pytest.mark.asyncio
async def test_status_remote_without_endpoint(tts_env, monkeypatch):

    def no_model():
        return None

    monkeypatch.setattr(speak, "find_model_dir", no_model)
    monkeypatch.setattr(speak, "model_available", lambda: False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put("/api/config", json={"voice": {"tts_engine": "remote"}})
        body = (await client.get("/api/tts/status")).json()
    assert body["engine"] == "remote"
    assert body["available"] is False  # nothing to narrate with yet


# ---- 5. /api/tts/synthesize wires the active engine ----


@pytest.mark.asyncio
async def test_synthesize_local_engine_still_local(tts_env, monkeypatch):
    """engine=local: bit-identical behavior (stubbed synth, WAV out)."""

    def fake_synth(text, voice="af_heart", speed=1.0, epoch=None):
        return b"\x00\x01" * 2400, 24000

    monkeypatch.setattr(speak, "synthesize", fake_synth)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put("/api/config", json={"voice": {"tts_engine": "local"}})
        res = await client.post("/api/tts/synthesize", json={"text": "Hello."})
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(res.content), "rb") as w:
        assert w.getframerate() == 24000


@pytest.mark.asyncio
async def test_synthesize_remote_engine_uses_remote(tts_env, monkeypatch):
    """engine=remote: the chunk route proxies to the remote engine; the
    WAV contract to the frontend is unchanged (the remote payload is
    already WAV, so it is piped through byte-for-byte)."""

    async def fake_remote(text, endpoint, api_key, model, voice, speed):
        return _wav_bytes(b"\x03\x04" * 2400)

    monkeypatch.setattr(speak, "synthesize_remote", fake_remote)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={"voice": {"tts_engine": "remote", "tts_endpoint": "http://herp.local:8081"}},
        )
        res = await client.post(
            "/api/tts/synthesize", json={"text": "Hello.", "voice": "af_heart", "epoch": 3}
        )
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"

    # Voice/speed default from the stored tts_* settings.
    captured = {}

    async def fake_remote(text, endpoint, api_key, model, voice, speed):
        captured.update(
            text=text, endpoint=endpoint, api_key=api_key, model=model, voice=voice, speed=speed
        )
        return _wav_bytes()

    monkeypatch.setattr(speak, "synthesize_remote", fake_remote)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local:8081",
                    "tts_api_key": "sk-live",
                    "tts_model": "kokoro",
                    "tts_voice": "zf_xiaobei",
                    "tts_speed": 0.9,
                }
            },
        )
        res = await client.post(
            "/api/tts/synthesize", json={"text": "Hello.", "epoch": 1}
        )
    assert res.status_code == 200
    assert captured == {
        "text": "Hello.",
        "endpoint": "http://herp.local:8081",
        "api_key": "sk-live",
        "model": "kokoro",
        "voice": "zf_xiaobei",
        "speed": 0.9,
    }


@pytest.mark.asyncio
async def test_synthesize_remote_409_without_endpoint(tts_env, monkeypatch):
    """Remote selected but unconfigured: a 409 that tells the user where
    to fix it (not the model-download offer)."""

    def no_model():
        return None

    monkeypatch.setattr(speak, "model_available", lambda: False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put("/api/config", json={"voice": {"tts_engine": "remote"}})
        res = await client.post("/api/tts/synthesize", json={"text": "Hello.", "epoch": 1})
    assert res.status_code == 409
    assert "endpoint" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_synthesize_remote_maps_error_to_502(tts_env, monkeypatch):
    """A remote failure surfaces its message; the frontend degrades to the
    visible text (and Settings' Test button shows the same detail)."""

    async def boom(text, endpoint, api_key, model, voice, speed):
        raise speak.RemoteTTSError("Incorrect API key")

    monkeypatch.setattr(speak, "synthesize_remote", boom)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={"voice": {"tts_engine": "remote", "tts_endpoint": "http://box"}},
        )
        res = await client.post("/api/tts/synthesize", json={"text": "Hello.", "epoch": 1})
    assert res.status_code == 502
    assert "Incorrect API key" in res.json()["detail"]


# ---- supersede floor applies in remote mode too ----


@pytest.mark.asyncio
async def test_synthesize_remote_superseded_maps_to_409(tts_env, monkeypatch):
    """stop/supersede must hold for remote synthesis: the floor check
    happens before any network call burns the endpoint."""
    called = {"remote": False}

    async def fake_remote(text, endpoint, api_key, model, voice, speed):
        called["remote"] = True
        return _wav_bytes()

    monkeypatch.setattr(speak, "synthesize_remote", fake_remote)
    monkeypatch.setattr(speak, "_floor", 10)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={"voice": {"tts_engine": "remote", "tts_endpoint": "http://box"}},
        )
        res = await client.post("/api/tts/synthesize", json={"text": "Hello.", "epoch": 3})
    assert res.status_code == 409
    assert called["remote"] is False

# ---- Settings Test button (/api/tts/test) ----


@pytest.mark.asyncio
async def test_tts_test_uses_active_engine(tts_env, monkeypatch):
    """The Test button synthesizes one short line through the ACTIVE
    engine with the drafts' voice/speed."""
    captured = {}

    async def fake_remote(text, endpoint, api_key, model, voice, speed):
        captured.update(text=text, endpoint=endpoint, voice=voice, speed=speed)
        return _wav_bytes()

    monkeypatch.setattr(speak, "synthesize_remote", fake_remote)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={"voice": {"tts_engine": "remote", "tts_endpoint": "http://herp.local:8081"}},
        )
        res = await client.post("/api/tts/test", json={"voice": "bm_george", "speed": 1.2})
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert captured["voice"] == "bm_george"
    assert captured["speed"] == 1.2
    assert captured["endpoint"] == "http://herp.local:8081"
    assert "working" in captured["text"].lower()


@pytest.mark.asyncio
async def test_tts_test_surfaces_remote_error(tts_env, monkeypatch):
    """A remote failure reaches the user verbatim — this is the whole
    point of the button."""

    async def boom(text, endpoint, api_key, model, voice, speed):
        raise speak.RemoteTTSError("HTTP 401: Incorrect API key")

    monkeypatch.setattr(speak, "synthesize_remote", boom)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={"voice": {"tts_engine": "remote", "tts_endpoint": "http://box"}},
        )
        res = await client.post("/api/tts/test", json={"voice": "af_heart", "speed": 1.0})
    assert res.status_code == 502
    assert "Incorrect API key" in res.json()["detail"]


@pytest.mark.asyncio
async def test_tts_test_local_engine_failure_surfaces(tts_env, monkeypatch):
    def bad_synth(*a, **k):
        raise RuntimeError("engine failed to start")

    monkeypatch.setattr(speak, "synthesize", bad_synth)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put("/api/config", json={"voice": {"tts_engine": "local"}})
        res = await client.post("/api/tts/test", json={"voice": "af_heart", "speed": 1.0})
    assert res.status_code == 503
    assert "engine failed" in res.json()["detail"]
