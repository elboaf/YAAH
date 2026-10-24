"""#443: the MOSS job API as a remote read-aloud flavor.

The owner-run MOSS-TTS server (herp.local) is NOT OpenAI-compatible: it
speaks an async job API - POST /api/moss/generate (urlencoded form;
wait=true inlines the WAV, wait=false returns a job id), a /jobs MAP
(no single-job GET), /jobs/{jid}/download, /jobs/{jid} DELETE, /refs,
/health. All verified against the live server's OpenAPI; this module
runs everything against a fake transport, never the network.

The openai flavor (test_issue_205_remote_tts.py) is untouched: its
defaults keep every pre-#443 config narrating exactly as before.
"""

from __future__ import annotations

import io
import json
import urllib.parse
import wave

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from backend.agent import config as config_mod
from backend.agent import speak
from backend.main import app


def _wav_bytes(pcm: bytes = b"\x00\x01" * 2400, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


WAV = _wav_bytes()


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


@pytest.fixture(autouse=True)
def clean_floor():
    """Reset the module-level supersede floor around every test (#317)."""
    speak._floor = 0
    yield
    speak._floor = 0


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_PATH to a per-test file (the #140 pattern)."""
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")


# The REAL client class, captured before any test patches speak.httpx.
_RealAsyncClient = httpx.AsyncClient


class _RecordingTransport(httpx.MockTransport):
    """Captures each request; handler returns per-attempt responses."""

    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.handler(request)


def _client(transport) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=transport)


def _form(request: httpx.Request) -> dict:
    """Parse an urlencoded POST body the MOSS server receives."""
    return {
        k: v[0]
        for k, v in urllib.parse.parse_qs(request.content.decode("utf-8")).items()
    }


def _moss_mocks(monkeypatch):
    """The AsyncClient patch every transport test needs."""
    holder: dict = {}

    def install(handler):
        transport = _RecordingTransport(handler)
        monkeypatch.setattr(
            speak.httpx, "AsyncClient", lambda **kw: _client(transport)
        )
        holder["transport"] = transport
        return transport

    holder["install"] = install
    return holder


# ---- 1. the inline (wait=true) contract -------------------------------------


async def test_moss_wait_true_returns_inline_wav(monkeypatch):
    """wait=true: one urlencoded POST, the WAV comes back inline."""
    mocks = _moss_mocks(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/moss/generate"
        form = _form(request)
        assert form["text"] == "Voice check."
        assert form["lang"] == "en"
        assert form["wait"] == "true"
        assert "ref" not in form  # reference-less by default
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)

    mocks["install"](handler)
    data = await speak.synthesize_moss("Voice check.", endpoint="http://herp.local")
    assert data == WAV


async def test_moss_ref_and_lang_ride_the_form(monkeypatch):
    """A configured ref clip and a non-default lang land in the form; the
    saved voice name is tolerated as a ref alias (the MOSS 'voice' IS a
    ref clip)."""

    def handler(request: httpx.Request) -> httpx.Response:
        form = _form(request)
        if form["text"] == "x":
            assert form["ref"] == "mes"
            assert form["lang"] == "zh"
        else:  # the alias call: ref from the voice channel, default lang
            assert form["ref"] == "mes"
            assert form["lang"] == "en"
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)

    mocks = _moss_mocks(monkeypatch)
    mocks["install"](handler)
    await speak.synthesize_moss(
        "x", endpoint="http://box", lang="zh", ref="mes"
    )
    await speak.synthesize_moss("y", endpoint="http://box", lang="en", voice="mes")


async def test_moss_url_base_tolerance(monkeypatch):
    """Bare host, trailing slash, pasted /api/moss root or full path all
    resolve to the same generate URL (never double-prefixing)."""
    seen: list[str] = []
    mocks = _moss_mocks(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)

    mocks["install"](handler)
    for base in (
        "http://box",
        "http://box/",
        "http://box/api/moss",
        "http://box/api/moss/",
        "http://box/api/moss/generate",
    ):
        await speak.synthesize_moss("x", endpoint=base)
    assert seen == ["http://box/api/moss/generate"] * 5


async def test_moss_requires_endpoint(monkeypatch):
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_moss("x", endpoint="  ")
    assert ei.value.code == "not-configured"


async def test_moss_unknown_ref_is_a_final_404(monkeypatch):
    """Unknown ref -> the server's 404 detail, ONE request (4xx is final,
    no retry): the user's stale ref name must be surfaced, not masked."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "ref 'af_heart' not found"})

    mocks = _moss_mocks(monkeypatch)
    transport = mocks["install"](handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_moss("x", endpoint="http://box", ref="af_heart")
    assert "af_heart" in str(ei.value)
    assert len(transport.requests) == 1


# ---- 2. the async job flow (wait rejected or stalled) -----------------------


async def test_moss_falls_back_to_job_flow_on_5xx(monkeypatch):
    """A 5xx on the wait POST falls back to submit -> poll the jobs MAP ->
    download -> DELETE, on the same client (no second connection)."""
    mocks = _moss_mocks(monkeypatch)
    jid = "mossabc123"

    def handler(request: httpx.Request) -> httpx.Response:
        form = _form(request)
        if request.method == "POST":
            if form.get("wait") == "true":
                return httpx.Response(500, json={"detail": "worker wedged"})
            assert form["wait"] == "false"
            return httpx.Response(200, json={"job_id": jid, "status": "queued"})
        if request.url.path.endswith("/jobs"):
            return httpx.Response(
                200,
                json={
                    "other": {"status": "done"},
                    jid: {"status": "done", "wav": f"/api/moss/jobs/{jid}/download"},
                },
            )
        if request.url.path.endswith("/download"):
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    transport = mocks["install"](handler)
    data = await speak.synthesize_moss("x", endpoint="http://box")
    assert data == WAV
    methods = [(r.method, r.url.path) for r in transport.requests]
    assert methods == [
        ("POST", "/api/moss/generate"),
        ("POST", "/api/moss/generate"),
        ("GET", "/api/moss/jobs"),
        ("GET", f"/api/moss/jobs/{jid}/download"),
        ("DELETE", f"/api/moss/jobs/{jid}"),
    ]


async def test_moss_async_server_never_inlines(monkeypatch):
    """A server that ignores wait=true (always answers JSON) still lands
    the audio through the job flow; the job is deleted afterwards."""
    mocks = _moss_mocks(monkeypatch)
    jid = "mossdeadbeef"
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/moss/generate"
            return httpx.Response(200, json={"job_id": jid, "status": "queued"})
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={jid: {"status": "done"}})
        if request.url.path.endswith("/download"):
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)
        if request.method == "DELETE":
            deleted.append(str(request.url))
            return httpx.Response(200, json={})
        raise AssertionError("unreachable")

    mocks["install"](handler)
    data = await speak.synthesize_moss("x", endpoint="http://box")
    assert data == WAV
    assert deleted == [f"http://box/api/moss/jobs/{jid}"]


async def test_moss_job_error_status_surfaces(monkeypatch):
    """A job that ends in error carries the job's error text; no download
    is attempted."""
    mocks = _moss_mocks(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "j1", "status": "queued"})
        if request.url.path.endswith("/jobs"):
            return httpx.Response(
                200, json={"j1": {"status": "error", "error": "OOM in vocoder"}}
            )
        raise AssertionError("download must not be reached")

    mocks["install"](handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_moss("x", endpoint="http://box")
    assert "OOM" in str(ei.value)


async def test_moss_polls_until_done(monkeypatch):
    """queued -> running -> done across polls; only OUR job id is read."""
    mocks = _moss_mocks(monkeypatch)
    jid = "jpoll"
    polls = {"n": 0}
    real_sleep = __import__("asyncio").sleep

    async def fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": jid, "status": "queued"})
        if request.url.path.endswith("/jobs"):
            polls["n"] += 1
            status = ("queued", "running", "done")[min(polls["n"], 3) - 1]
            return httpx.Response(200, json={jid: {"status": status}})
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)

    mocks["install"](handler)
    data = await speak.synthesize_moss("x", endpoint="http://box")
    assert data == WAV
    assert polls["n"] == 3


async def test_moss_poll_timeout_raises(monkeypatch):
    """A job that never finishes raises inside the poll budget (the
    budget is patched small so the test stays instant)."""
    mocks = _moss_mocks(monkeypatch)
    real_sleep = __import__("asyncio").sleep

    async def fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    monkeypatch.setattr(speak, "MOSS_POLL_TIMEOUT", 1.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "jx", "status": "queued"})
        return httpx.Response(200, json={"jx": {"status": "running"}})

    mocks["install"](handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_moss("x", endpoint="http://box")
    assert "timed out" in str(ei.value)


async def test_moss_wait_timeout_retries_then_raises(monkeypatch):
    """A dead server: one retry on the inline POST, then the error
    surfaces (the frontend skips the chunk; text stays visible)."""
    calls = {"n": 0}
    mocks = _moss_mocks(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("timed out")

    mocks["install"](handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_moss("x", endpoint="http://box")
    assert calls["n"] == 2
    assert "timed out" in str(ei.value)


async def test_moss_job_flow_transport_failure_is_terminal(monkeypatch):
    """A transport failure mid-job-flow must NOT restart the flow: a
    retried flow would synthesize the same chunk twice server-side."""
    posts = {"n": 0}
    mocks = _moss_mocks(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts["n"] += 1
            return httpx.Response(200, json={"job_id": "jt", "status": "queued"})
        raise httpx.ConnectTimeout("poll died")

    mocks["install"](handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_moss("x", endpoint="http://box")
    assert "poll died" in str(ei.value)
    assert posts["n"] == 1  # wait=true envelope: no duplicate wait=false submit


async def test_moss_empty_download_raises(monkeypatch):
    """An empty 200 download is a broken server, not silence."""
    mocks = _moss_mocks(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"job_id": "je", "status": "queued"})
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"je": {"status": "done"}})
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=b"")

    mocks["install"](handler)
    with pytest.raises(speak.RemoteTTSError) as ei:
        await speak.synthesize_moss("x", endpoint="http://box")
    assert "empty" in str(ei.value)


# ---- 3. refs + health probes -------------------------------------------------


async def test_moss_refs_parsed(monkeypatch):
    """GET /api/moss/refs -> the clip names, junk dropped, order kept."""
    mocks = _moss_mocks(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/moss/refs"
        return httpx.Response(
            200,
            json=[
                {"name": "ardy", "secs": 42.0},
                {"name": "mes", "secs": 19.4},
                {"name": "", "secs": 1.0},
                "bare-name",
            ],
        )

    mocks["install"](handler)
    names = await speak.probe_moss_refs("http://box")
    assert names == ["ardy", "mes", "bare-name"]


async def test_moss_refs_failure_reads_empty(monkeypatch):
    """ANY probe failure - 404, timeout - reads [] (free-text fallback,
    the same posture as the OpenAI voices probe)."""
    mocks = _moss_mocks(monkeypatch)
    mocks["install"](lambda request: httpx.Response(404, json={"detail": "no"}))
    assert await speak.probe_moss_refs("http://box") == []

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("down")

    mocks["install"](boom)
    assert await speak.probe_moss_refs("http://box") == []


async def test_moss_health_ready_and_failures(monkeypatch):
    """ready/ok truthy -> True; 404 / transport / garbage -> False."""
    mocks = _moss_mocks(monkeypatch)
    mocks["install"](
        lambda request: httpx.Response(
            200, json={"ok": True, "weights_ok": True, "ready": True}
        )
    )
    assert await speak.probe_moss_health("http://box") is True

    mocks["install"](lambda request: httpx.Response(200, json={"ok": True}))
    assert await speak.probe_moss_health("http://box") is True  # ok fallback

    mocks["install"](lambda request: httpx.Response(404, json={"detail": "no"}))
    assert await speak.probe_moss_health("http://box") is False

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("down")

    mocks["install"](boom)
    assert await speak.probe_moss_health("http://box") is False


# ---- 4. the endpoints learn the flavor ---------------------------------------


async def test_synthesize_moss_flavor_wiring(tts_env, monkeypatch):
    """tts_remote_api=moss: the synthesize route proxies to the MOSS
    engine with the stored lang/ref; the WAV contract to the frontend is
    unchanged (byte-for-byte passthrough)."""
    captured: dict = {}

    async def fake_moss(text, endpoint, lang="en", ref="", voice=""):
        captured.update(text=text, endpoint=endpoint, lang=lang, ref=ref, voice=voice)
        return WAV

    monkeypatch.setattr(speak, "synthesize_moss", fake_moss)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local",
                    "tts_remote_api": "moss",
                    "tts_moss_lang": "en",
                    "tts_moss_ref": "mes",
                    "tts_voice": "mes",
                }
            },
        )
        res = await client.post("/api/tts/synthesize", json={"text": "Hello.", "epoch": 3})
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/wav"
    assert res.content == WAV
    assert captured == {
        "text": "Hello.",
        "endpoint": "http://herp.local",
        "lang": "en",
        "ref": "mes",
        "voice": "mes",
    }


async def test_synthesize_moss_409_without_endpoint(tts_env):
    """MOSS flavor selected but unconfigured: the shared not-configured
    409 (not the model-download offer)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={"voice": {"tts_engine": "remote", "tts_remote_api": "moss"}},
        )
        res = await client.post("/api/tts/synthesize", json={"text": "Hello.", "epoch": 1})
    assert res.status_code == 409
    assert "endpoint" in res.json()["detail"].lower()


async def test_synthesize_moss_error_maps_to_502(tts_env, monkeypatch):
    async def boom(text, endpoint, lang="en", ref="", voice=""):
        raise speak.RemoteTTSError("ref 'ghost' not found")

    monkeypatch.setattr(speak, "synthesize_moss", boom)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local",
                    "tts_remote_api": "moss",
                }
            },
        )
        res = await client.post("/api/tts/synthesize", json={"text": "Hello.", "epoch": 1})
    assert res.status_code == 502
    assert "ghost" in res.json()["detail"]


async def test_openai_flavor_is_untouched(tts_env, monkeypatch):
    """Without tts_remote_api (or with openai), the #205 path runs
    byte-for-byte: synthesize_remote gets the same args as before."""
    calls: dict = {}

    async def fake_remote(text, endpoint, api_key, model, voice, speed):
        calls.update(endpoint=endpoint, model=model, voice=voice)
        return WAV

    monkeypatch.setattr(speak, "synthesize_remote", fake_remote)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local:8081",
                    "tts_model": "kokoro",
                    "tts_voice": "af_heart",
                }
            },
        )
        res = await client.post("/api/tts/synthesize", json={"text": "Hello.", "epoch": 2})
    assert res.status_code == 200
    assert calls == {
        "endpoint": "http://herp.local:8081",
        "model": "kokoro",
        "voice": "af_heart",
    }


async def test_test_endpoint_sends_sample_verbatim(tts_env, monkeypatch):
    """Settings' Test button through the MOSS flavor sends the sample
    as-is: the live server synthesizes short text fine, and padding would
    only slow the button down."""
    captured: dict = {}

    async def fake_moss(text, endpoint, lang="en", ref="", voice=""):
        captured.update(text=text, endpoint=endpoint)
        return WAV

    monkeypatch.setattr(speak, "synthesize_moss", fake_moss)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local",
                    "tts_remote_api": "moss",
                }
            },
        )
        res = await client.post("/api/tts/test", json={})
    assert res.status_code == 200
    assert captured["text"] == "Voice test. If you can hear this, narration is working."
    assert captured["endpoint"] == "http://herp.local"


async def test_voices_endpoint_lists_moss_refs(tts_env, monkeypatch):
    """Voice discovery for the moss flavor lists the server's reference
    clips; the openai flavor still probes /voices."""
    async def fake_refs(endpoint, api_key=""):
        assert endpoint == "http://herp.local"
        return ["ardy", "mes"]

    async def fake_openai(endpoint, api_key=""):
        return ["af_heart"]

    monkeypatch.setattr(speak, "probe_moss_refs", fake_refs)
    monkeypatch.setattr(speak, "probe_remote_voices", fake_openai)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local",
                    "tts_remote_api": "moss",
                }
            },
        )
        res = await client.post("/api/tts/voices", json={})
        assert res.status_code == 200
        assert res.json()["voices"] == ["ardy", "mes"]

        await client.put(
            "/api/config", json={"voice": {"tts_remote_api": "openai"}}
        )
        res = await client.post("/api/tts/voices", json={})
        assert res.json()["voices"] == ["af_heart"]


async def test_status_reports_flavor_and_moss_health(tts_env, monkeypatch):
    """status: remote_api echoes the flavor; moss availability = endpoint
    set AND health ready; the openai flavor never probes health."""
    health_calls: dict = {}

    async def fake_health(endpoint, api_key=""):
        health_calls["endpoint"] = endpoint
        return True

    monkeypatch.setattr(speak, "probe_moss_health", fake_health)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local",
                    "tts_remote_api": "moss",
                }
            },
        )
        s = (await client.get("/api/tts/status")).json()
        assert s["remote_api"] == "moss"
        assert s["available"] is True
        assert health_calls == {"endpoint": "http://herp.local"}

        # A dead MOSS server reads unavailable (still no exception).
        async def dead(endpoint, api_key=""):
            return False

        monkeypatch.setattr(speak, "probe_moss_health", dead)
        s = (await client.get("/api/tts/status")).json()
        assert s["available"] is False

        # The openai flavor: no health probe at all.
        async def forbidden(endpoint, api_key=""):
            raise AssertionError("openai flavor must not probe moss health")

        monkeypatch.setattr(speak, "probe_moss_health", forbidden)
        await client.put(
            "/api/config", json={"voice": {"tts_remote_api": "openai"}}
        )
        s = (await client.get("/api/tts/status")).json()
        assert s["remote_api"] == "openai"
        assert s["available"] is True  # endpoint set is enough


# ---- 5. config ----------------------------------------------------------------


def test_voice_defaults_keep_openai_flavor():
    """The #443 keys exist with back-compat defaults: every config written
    before #443 keeps narrating through the openai flavor."""
    d = config_mod.DEFAULTS["voice"]
    assert d["tts_remote_api"] == "openai"
    assert d["tts_moss_lang"] == "en"
    assert d["tts_moss_ref"] == ""


async def test_config_roundtrip_moss_keys():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            "/api/config",
            json={
                "voice": {
                    "tts_engine": "remote",
                    "tts_endpoint": "http://herp.local",
                    "tts_remote_api": "moss",
                    "tts_moss_lang": "zh",
                    "tts_moss_ref": "mes",
                    "tts_voice": "mes",
                }
            },
        )
        got = (await client.get("/api/config")).json()
    v = got["voice"]
    assert v["tts_remote_api"] == "moss"
    assert v["tts_moss_lang"] == "zh"
    assert v["tts_moss_ref"] == "mes"
    assert v["tts_voice"] == "mes"
