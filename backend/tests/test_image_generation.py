"""Image generation via a stable-diffusion.cpp server (sd-server).

Covers the v2 design: a dedicated `image_generation` config block (never
a chat providers entry), capability-aware resolution (the SERVER's
limits decide; oversized requests generate native + ESRGAN upscale via
the native /sdcpp/v1/upscale endpoint), generate_image + upscale_image
tools gated on api_base being configured, display:"inline" results that
render in the tape but never vision-replay, local-only workspaces, and
main-chat-only visibility.
"""
import asyncio
import base64
import json
import struct

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agent import config as cfgmod
from backend.agent import subagents, tools


PNG_BYTES = b"\x89PNG-fake-image"


def _png(w, h):
    """Minimal PNG-shaped blob with real IHDR dimensions."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", w, h)
        + b"0" * 16
    )


class _FakeResp:
    status_code = 200
    text = '{"error": "fake-response"}'

    def json(self):
        return {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]}


class _FakeClient:
    last = None
    caps_payload = {}
    upscale_payload = {}
    gen_calls = 0
    caps_calls = 0
    upscale_calls = 0
    gen_sizes = []
    upscale_bodies = []
    job_bodies = []
    job_status = None  # None = complete normally
    cancel_calls = 0
    native_ok = True
    reject_hires_submit = False  # 400 any img_gen body carrying `hires`
    submit_calls = 0
    submit_status = 202
    submit_payload = None  # when set, the submit responds with this body
    poll_queue = []  # poll payloads replayed before the normal status
    poll_attempts = 0

    @classmethod
    def reset(cls):
        cls.caps_payload = {}
        cls.upscale_payload = {}
        cls.gen_calls = 0
        cls.caps_calls = 0
        cls.upscale_calls = 0
        cls.gen_sizes = []
        cls.upscale_bodies = []
        cls.job_bodies = []
        cls.job_status = None
        cls.cancel_calls = 0
        cls.native_ok = True
        cls.reject_hires_submit = False
        cls.submit_calls = 0
        cls.submit_status = 202
        cls.submit_payload = None
        cls.poll_queue = []
        cls.poll_attempts = 0

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if "/capabilities" in url:
            _FakeClient.caps_calls += 1
            r = _FakeResp()
            r.json = lambda: dict(_FakeClient.caps_payload)
            return r
        # job poll: replay scripted payloads first (e.g. a transient
        # hiccup before the real status), then the normal status machine
        if _FakeClient.poll_queue:
            _FakeClient.poll_attempts += 1
            payload = _FakeClient.poll_queue.pop(0)
            if isinstance(payload, int):  # a non-200 status code
                r = _FakeResp()
                r.status_code = payload
                r.json = lambda: {"error": "boom"}
                return r
            r = _FakeResp()
            r.status_code = 200
            r.json = lambda: dict(payload)
            return r
        if _FakeClient.job_status == "generating":
            payload = {"status": "generating", "result": None, "error": None}
        elif _FakeClient.job_status == "failed":
            payload = {
                "status": "failed",
                "result": None,
                "error": {"code": "generation_failed", "message": "boom"},
            }
        else:
            job = _FakeClient.job_bodies[-1] if _FakeClient.job_bodies else {}
            # a hires job completes at the HIRES TARGET dims (the second
            # pass runs there), mirroring the real server
            hires = job.get("hires") or {}
            payload = {
                "status": "completed",
                "result": {
                    "output_format": "png",
                    "images": [
                        {
                            "index": 0,
                            "b64_json": base64.b64encode(
                                _png(
                                    hires.get("target_width")
                                    or job.get("width")
                                    or 512,
                                    hires.get("target_height")
                                    or job.get("height")
                                    or 512,
                                )
                            ).decode(),
                        }
                    ],
                },
                "error": None,
            }
        r = _FakeResp()
        r.json = lambda: dict(payload)
        return r

    async def post(self, url, json=None):
        _FakeClient.last = (url, json)
        if url.endswith("/sdcpp/v1/img_gen"):
            _FakeClient.submit_calls += 1
            if not _FakeClient.native_ok:
                r = _FakeResp()
                r.status_code = 404
                r.json = lambda: {"error": "not found"}
                return r
            if _FakeClient.reject_hires_submit and "hires" in (json or {}):
                r = _FakeResp()
                r.status_code = 400
                r.json = lambda: {"error": {"message": "invalid generation parameters"}}
                return r
            if _FakeClient.submit_payload is not None:
                # scripted submit response (e.g. malformed 200 body)
                r = _FakeResp()
                r.status_code = _FakeClient.submit_status
                r.json = lambda: _FakeClient.submit_payload
                return r
            _FakeClient.job_bodies.append(dict(json or {}))
            r = _FakeResp()
            r.json = lambda: {"id": "job_test1", "status": "queued"}
            return r
        if url.endswith("/cancel"):
            _FakeClient.cancel_calls += 1
            r = _FakeResp()
            r.json = lambda: {"ok": True}
            return r
        if url.endswith("/sdcpp/v1/upscale"):
            _FakeClient.upscale_calls += 1
            _FakeClient.upscale_bodies.append(json)
            r = _FakeResp()
            r.json = lambda: dict(_FakeClient.upscale_payload)
            return r
        # legacy sync generation endpoint
        _FakeClient.gen_calls += 1
        _FakeClient.gen_sizes.append((json or {}).get("size"))
        return _FakeResp()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Isolated config with an sd-server configured."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    _FakeClient.reset()
    tools._CAPS_CACHE.clear()
    cfgmod.save_config(
        {"image_generation": {"provider": "sdcpp", "api_base": "http://127.0.0.1:1234"}}
    )
    return cfgmod.load_config()


def _schema_names(workspace=None):
    return [s["function"]["name"] for s in tools.get_schemas(workspace=workspace)]


# ------------------------------------------------------------- config block


def test_image_generation_block_roundtrips(configured):
    """The dedicated block persists and never leaks into chat providers."""
    cfg = configured
    assert cfg["image_generation"]["api_base"] == "http://127.0.0.1:1234"
    assert "sdcpp" not in cfg["providers"]
    assert cfg["active_provider"] != "sdcpp"


def test_partial_save_keeps_sibling_keys(client):
    """A save touching only provider must not wipe api_base (API-level
    block merge, same rule as the remote/memory blocks)."""
    r = client.put(
        "/api/config",
        json={"image_generation": {"api_base": "http://127.0.0.1:1234"}},
    )
    assert r.status_code == 200
    r = client.put("/api/config", json={"image_generation": {"provider": "sdcpp"}})
    assert r.status_code == 200
    block = client.get("/api/config").json()["image_generation"]
    assert block["api_base"] == "http://127.0.0.1:1234"
    assert block["provider"] == "sdcpp"


# ------------------------------------------------------------ tool gating


def test_schema_hidden_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    names = _schema_names()
    assert "generate_image" not in names and "upscale_image" not in names


def test_schema_present_when_configured(configured):
    names = _schema_names()
    assert "generate_image" in names and "upscale_image" in names


def test_execute_tool_refused_when_unconfigured(tmp_path, monkeypatch):
    """Memory/sandbox posture: config vanished mid-run -> refuse, point at
    Settings, name no config keys."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    result = asyncio.run(
        tools.execute_tool("upscale_image", {"path": "x"}, workspace=str(tmp_path))
    )
    assert "Settings" in result["info"]
    assert "api_base" not in result["info"]


def test_sub_agents_never_get_image_tools(configured):
    """Main-chat-only: even configured, neither tool reaches any
    sub-agent's resolved schema."""
    defn = subagents.AgentDef(name="x", description="", body="", tools=None)
    resolved = subagents._resolve_tools(defn, workspace=None)
    names = [s["function"]["name"] for s in resolved]
    assert "generate_image" not in names and "upscale_image" not in names


# --------------------------------------------------- capability-aware sizes


def test_no_size_requested_uses_built_in_defaults(configured, tmp_path, monkeypatch):
    """#419, caps-less legacy transport: the omit path is never
    size/sampling-blind - the built-in 1024x1024 rides the legacy size
    string (pre-#419 this was None = server default 512)."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert not result.get("error"), result
    assert _FakeClient.gen_sizes == ["1024x1024"]


def test_above_budget_size_renders_natively(configured, tmp_path, monkeypatch):
    """#421 DEFAULT POLICY: a 1024x1024 request on a 512-default server
    renders NATIVELY at 1024 (within the server's generation limits) -
    the first pass decides composition (at a 512 render klein-class
    models drop subjects; a hires second pass cannot resurrect what
    pass 1 never drew). No hires body, no ESRGAN calls."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096,
                   "max_upscale_width": 8192, "max_upscale_height": 8192},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
        "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "width": 1024, "height": 1024},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    jb = _FakeClient.job_bodies[0]
    assert jb["width"] == 1024 and jb["height"] == 1024
    assert "hires" not in jb
    assert _FakeClient.upscale_calls == 0
    assert result["width"] == 1024 and result["height"] == 1024
    assert "native render at 1024x1024" in result["note"]


def test_explicit_hires_fix_true_keeps_conservative_route(configured, tmp_path, monkeypatch):
    """#421: hires_fix=True is the explicit OPT-IN to the pre-#421
    conservative policy: render within the default budget (512-class)
    and let the server's hires fix produce the exact final size.
    Protects 12GB-class cards from weight-thrash when the operator
    asks for it."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096,
                   "max_upscale_width": 8192, "max_upscale_height": 8192},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
        "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {"prompt": "x", "width": 1024, "height": 1024, "hires_fix": True},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    # first diffusion pass stays at the default budget, NEVER 1024
    assert _FakeClient.job_bodies[0]["width"] == 512
    assert _FakeClient.job_bodies[0]["height"] == 512
    # hires rides in the SAME job body: exact target, no client-side chain
    jb = _FakeClient.job_bodies[0]
    assert jb["hires"]["enabled"] is True
    assert jb["hires"]["target_width"] == 1024
    assert jb["hires"]["target_height"] == 1024
    assert jb["vae_tiling_params"]["enabled"] is True
    assert _FakeClient.upscale_calls == 0
    assert result["width"] == 1024 and result["height"] == 1024
    assert "hires fix" in result["note"] and "512x512" in result["note"]


def test_beyond_limits_with_hires_fix_off_upscales_instead_of_diffusing(configured, tmp_path, monkeypatch):
    """#421: a target BEYOND the generation limits (8192-class request
    on a 4096-limited server) renders at the maximum legal size and
    reaches the request with the ESRGAN chain (fast, may overshoot).
    hires_fix=False is the natural mode here: the conservative route
    cannot help past the limits either."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096,
                   "max_upscale_width": 8192, "max_upscale_height": 8192},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
        "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    }
    _FakeClient.upscale_payload = {
        "images": [{"b64_json": base64.b64encode(_png(6144, 6144)).decode()}],
        "upscaler": "RealESRGAN_x4plus", "scale": 4, "repeats": 1,
        "width": 6144, "height": 6144,
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {"prompt": "x", "width": 6144, "height": 6144, "hires_fix": False},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    # the render hits the server's generation maximum, never the request
    assert _FakeClient.job_bodies[0]["width"] == 4096
    assert _FakeClient.job_bodies[0]["height"] == 4096
    assert "hires" not in _FakeClient.job_bodies[0]
    assert _FakeClient.upscale_calls == 1
    assert result["width"] == 6144 and result["height"] == 6144
    assert "exceeds the server" in result["note"]


def test_hires_without_vae_tiling_support_omits_tiling(configured, tmp_path, monkeypatch):
    """An older build that reports hires but not vae_tiling gets the
    hires object WITHOUT the tiling params (server would ignore/400)."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
        "features_by_mode": {"img_gen": {"hires": True}},
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {"prompt": "x", "width": 768, "height": 768, "hires_fix": True},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    jb = _FakeClient.job_bodies[0]
    assert jb["hires"]["enabled"] is True
    assert "vae_tiling_params" not in jb
    assert jb["hires"]["target_width"] == 768


def test_hires_refused_by_server_falls_back_with_a_note(configured, tmp_path, monkeypatch):
    """A server that 400s the hires body (unknown field, bad upscaler
    name): one clean retry WITHOUT hires, an honest note, and the
    ESRGAN chain still lands the size."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096,
                   "max_upscale_width": 8192, "max_upscale_height": 8192},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
        "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    }
    _FakeClient.upscale_payload = {
        "images": [{"b64_json": base64.b64encode(_png(1024, 1024)).decode()}],
        "upscaler": "RealESRGAN_x4plus", "scale": 4, "repeats": 1,
        "width": 1024, "height": 1024,
    }
    _FakeClient.reject_hires_submit = True
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {"prompt": "x", "width": 1024, "height": 1024, "hires_fix": True},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    # exactly two submits: the hires body, then the clean retry
    assert _FakeClient.submit_calls == 2
    assert "hires" not in _FakeClient.job_bodies[-1]
    assert "refused" in result["note"]
    # the chain still delivers the size
    assert _FakeClient.upscale_calls == 1
    assert result["width"] == 1024 and result["height"] == 1024


def test_hires_upscaler_name_comes_from_capabilities(configured, tmp_path, monkeypatch):
    """The hires pass uses the server's first image_upscale-capable
    upscaler from capabilities - never a hardcoded model name."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
        "upscalers": [
            {"name": "Lanczos", "image_upscale": False},
            {"name": "MyUpscaler_x2", "image_upscale": True},
        ],
        "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {"prompt": "x", "width": 1024, "height": 1024, "hires_fix": True},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    assert _FakeClient.job_bodies[0]["hires"]["upscaler"] == "MyUpscaler_x2"


def test_no_size_requested_rides_built_in_size(configured, tmp_path, monkeypatch):
    """#419 + #421: no width/height from caller or config -> the
    BUILT-IN 1024x1024 is the NATIVE render size (within limits):
    the server's own default stops deciding the omit path, and the
    first pass sees the full canvas."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
        "features_by_mode": {"img_gen": {"hires": True, "vae_tiling": True}},
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert not result.get("error"), result
    jb = _FakeClient.job_bodies[0]
    assert jb["width"] == 1024 and jb["height"] == 1024
    assert "hires" not in jb
    assert result["width"] == 1024 and result["height"] == 1024
    assert "size 1024x1024 (built-in default)" in (result.get("note") or "")


def test_explicit_size_within_limits_renders_natively(configured, tmp_path, monkeypatch):
    """#421: any explicit size within the server's limits IS the
    render size - no budget reshaping, no enlargement."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "width": 512, "height": 512},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    assert _FakeClient.job_bodies[0]["width"] == 512
    assert _FakeClient.job_bodies[0]["height"] == 512
    assert _FakeClient.upscale_calls == 0


def test_beyond_limits_without_esrgan_renders_max_legal_and_says_so(configured, tmp_path, monkeypatch):
    """#421: a 6144x6144 request on a 4096-limited, ESRGAN-less server
    renders at the maximum legal size (not the 512 default) and says
    so."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096},
        "defaults": {"width": 512, "height": 512},
        "upscale": False,
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "width": 6144, "height": 6144},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    assert _FakeClient.job_bodies[0]["width"] == 4096
    assert _FakeClient.upscale_calls == 0
    assert "no ESRGAN" in result["note"]
    assert "exceeds the server" in result["note"]


def test_beyond_limits_with_esrgan_enlarges_toward_request(configured, tmp_path, monkeypatch):
    """#421: a 6144x6144 request on a 4096-limited server WITH an
    upscaler renders 4096 natively, then enlarges toward the request
    with the ESRGAN chain."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096,
                   "max_upscale_width": 8192, "max_upscale_height": 8192},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
    }
    _FakeClient.upscale_payload = {
        "images": [{"b64_json": base64.b64encode(_png(6144, 6144)).decode()}],
        "upscaler": "RealESRGAN_x4plus", "scale": 2, "repeats": 1,
        "width": 6144, "height": 6144,
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "width": 6144, "height": 6144},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    assert _FakeClient.job_bodies[0]["width"] == 4096
    assert _FakeClient.upscale_calls == 1
    assert result["width"] == 6144 and result["height"] == 6144
    assert "exceeds the server" in result["note"]


def test_non_square_within_limits_renders_natively(configured, tmp_path, monkeypatch):
    """#421: 1536x768 (2:1) on a 512-default server renders NATIVELY
    at 1536x768 (within generation limits) - the full-canvas first
    pass decides composition; no budget reshaping, no upscale."""
    _FakeClient.caps_payload = {
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096,
                   "max_upscale_width": 8192, "max_upscale_height": 8192},
        "defaults": {"width": 512, "height": 512},
        "upscale": True,
    }
    _FakeClient.upscale_payload = {
        "images": [{"b64_json": base64.b64encode(_png(1536, 768)).decode()}],
        "upscaler": "RealESRGAN_x4plus", "scale": 2, "repeats": 1,
        "width": 1536, "height": 768,
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "width": 1536, "height": 768},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    jb = _FakeClient.job_bodies[0]
    assert jb["width"] == 1536 and jb["height"] == 768
    assert "hires" not in jb
    assert _FakeClient.upscale_calls == 0


def test_capabilities_failure_degrades_to_passthrough(configured, tmp_path, monkeypatch):
    """Older server builds without /sdcpp/v1/capabilities: the requested
    size passes through and the server decides - no invented limits."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "width": 1024, "height": 768},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    assert _FakeClient.gen_sizes == ["1024x768"]
    assert "native" not in result


def test_caps_cache_hits_within_ttl(configured, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    asyncio.run(tools._capabilities("http://127.0.0.1:1234"))
    asyncio.run(tools._capabilities("http://127.0.0.1:1234"))
    assert _FakeClient.caps_calls == 1


# --------------------------------------------------------------- executor


def test_generate_image_end_to_end(configured, tmp_path, monkeypatch):
    """One call: request shape, extra-args embedding, store rel for inline
    display, workspace copy under ai-images/."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {"prompt": "a cat", "negative_prompt": "dogs", "seed": 42},
            workspace=str(ws),
        )
    )
    url, body = _FakeClient.last
    assert url == "http://127.0.0.1:1234/v1/images/generations"
    assert body["n"] == 1
    assert "sd_cpp_extra_args" in body["prompt"]
    embedded = json.loads(
        body["prompt"].split("<sd_cpp_extra_args>")[1].split("</sd_cpp_extra_args>")[0]
    )
    # #419: sampling is always specified - built-in steps/cfg ride the
    # envelope even when the caller only named negative_prompt + seed
    assert embedded == {
        "negative_prompt": "dogs",
        "seed": 42,
        "sample_params": {"sample_steps": 4, "guidance": {"txt_cfg": 1.0}},
    }
    assert result["images"] and result["images"][0].startswith("generate_image/")
    assert result["path"].startswith("ai-images/")
    assert result["display"] == "inline"
    assert (ws / result["path"]).read_bytes() == PNG_BYTES


def test_generate_image_remote_workspace_local_only(configured, tmp_path, monkeypatch):
    """Remote sessions: no workspace copy, image still displays, refusal note."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x"}, workspace="remote:host1:C:/there"
        )
    )
    assert result["images"] and not result.get("path")
    assert "remote" in result["note"]


def test_generate_image_http_error_is_structured(configured, tmp_path, monkeypatch):
    class _Down(_FakeClient):
        async def post(self, url, json=None):
            class _R:
                status_code = 500

                def json(self):
                    return {}

            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", _Down)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert "HTTP 500" in result["error"]


# ------------------------------------------------------------ upscale tool


class _ChainClient(_FakeClient):
    """Multi-step chain: each call doubles the image (a x2 ESRGAN model);
    response reports the server's REAL scale and evolving dims."""

    @classmethod
    def reset(cls):
        super().reset()
        # a x2 model on a 256px source: one call -> 512, two -> 1024
        cls.chain = [(512, 512), (1024, 1024)]

    @classmethod
    async def post(cls, url, json=None):
        cls.last = (url, json)
        if url.endswith("/sdcpp/v1/upscale"):
            cls.upscale_calls += 1
            step = min(cls.upscale_calls, len(cls.chain)) - 1
            w, h = cls.chain[step]
            cls.upscale_bodies.append(json)
            payload = {
                "images": [{"b64_json": base64.b64encode(_png(w, h)).decode()}],
                "upscaler": "realesr-general",
                "scale": 2,
                "repeats": 1,
                "width": w,
                "height": h,
            }
            r = _FakeResp()
            r.json = lambda: dict(payload)
            return r
        cls.gen_calls += 1
        cls.gen_sizes.append((json or {}).get("size"))
        return _FakeResp()


def test_upscale_image_stops_at_target(configured, tmp_path, monkeypatch):
    """Staged chain: single-repeat steps, server's real scale honored,
    stops exactly at the target - no x2-per-repeat guessing, no overshoot."""
    _ChainClient.reset()
    _ChainClient.caps_payload = {"upscale": True}
    monkeypatch.setattr(httpx, "AsyncClient", _ChainClient)
    ws = tmp_path / "ws"
    (ws / "ai-images").mkdir(parents=True)
    (ws / "ai-images" / "src.png").write_bytes(_png(256, 256))
    result = asyncio.run(
        tools.execute_tool(
            "upscale_image",
            {"path": "ai-images/src.png", "target_width": 1024, "target_height": 1024},
            workspace=str(ws),
        )
    )
    assert not result.get("error"), result
    assert _ChainClient.upscale_calls == 2  # 256 -> 512 -> 1024
    assert all(b["repeats"] == 1 for b in _ChainClient.upscale_bodies)
    assert result["width"] == 1024 and result["height"] == 1024
    assert result["upscaler"] == "realesr-general"
    assert result["path"].startswith("ai-images/upscaled-")
    assert (ws / result["path"]).is_file()
    # original untouched
    assert (ws / "ai-images" / "src.png").read_bytes() == _png(256, 256)


def test_upscale_image_refuses_without_esrgan(configured, tmp_path, monkeypatch):
    _FakeClient.caps_payload = {"upscale": False}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "upscale_image", {"path": "nope.png"}, workspace=str(tmp_path)
        )
    )
    # gate fires before the file read: the actionable error is the
    # server-side one, not "image not found"
    assert "no ESRGAN" in result["error"]
    assert "not found" not in result["error"]
    assert _FakeClient.upscale_calls == 0


def test_upscale_image_rejects_paths_outside_workspace(configured, tmp_path, monkeypatch):
    """Security (CodeRabbit): path is model-supplied, so containment is
    enforced - traversal and absolute paths outside the workspace must
    not read arbitrary files and ship them to the server."""
    _FakeClient.caps_payload = {"upscale": True}
    _FakeClient.upscale_payload = {
        "images": [{"b64_json": base64.b64encode(_png(128, 128)).decode()}],
        "upscaler": "realesr-general", "scale": 2, "repeats": 1,
        "width": 128, "height": 128,
    }
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    ws = tmp_path / "ws"
    (ws / "ai-images").mkdir(parents=True)
    secret = tmp_path / "secret.png"
    secret.write_bytes(_png(64, 64))
    for bad in (
        "../secret.png",
        "..\\secret.png",
        str(secret),  # absolute, outside workspace
    ):
        result = asyncio.run(
            tools.execute_tool("upscale_image", {"path": bad}, workspace=str(ws))
        )
        assert "not found" in result.get("error", ""), (bad, result)
    assert _FakeClient.upscale_calls == 0
    # a legitimate in-workspace path still reads fine
    (ws / "ai-images" / "ok.png").write_bytes(_png(64, 64))
    ok = asyncio.run(
        tools.execute_tool(
            "upscale_image", {"path": "ai-images/ok.png", "repeats": 1}, workspace=str(ws)
        )
    )
    assert not ok.get("error"), ok


def test_upscale_image_missing_path_is_friendly(configured, tmp_path, monkeypatch):
    _FakeClient.caps_payload = {"upscale": True}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "upscale_image", {"path": "ai-images/ghost.png"}, workspace=str(tmp_path)
        )
    )
    assert "not found" in result["error"] and "ai-images" in result["error"]


# ------------------------------------------------- display-only replay rule


def _persist_generate_roundtrip(configured, tmp_path, monkeypatch):
    """One generate_image call persisted to the transcript; returns the
    stored tool-row content for replay tests."""
    import backend.main as mainmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    from backend.db.database import add_message, create_conversation, get_messages

    cid = asyncio.run(create_conversation("imgen-display", None))
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    asyncio.run(
        add_message(
            cid,
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "call_t1",
                    "type": "function",
                    "function": {"name": "generate_image", "arguments": "{}"},
                }
            ],
        )
    )
    asyncio.run(add_message(cid, "tool", json.dumps(result), tool_call_id="call_t1"))
    rows = asyncio.run(get_messages(cid))
    return [r for r in rows if r["role"] == "tool"][0]


def test_generated_image_not_replayed_as_vision(configured, tmp_path, monkeypatch):
    """The display fix: a generate_image tool row must NOT become image
    parts on the next turn's context - it replays as its text content
    only. Inspecting one is a deliberate view_image call."""
    row = _persist_generate_roundtrip(configured, tmp_path, monkeypatch)
    from backend.agent.loop import load_history

    msgs = asyncio.run(load_history(row["conversation_id"]))
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert isinstance(content, str), content
    assert "data:image" not in content
    parsed = json.loads(content)
    assert parsed["display"] == "inline"
    assert parsed["path"].startswith("ai-images/")


def test_view_image_results_still_replay_as_vision(configured, tmp_path, monkeypatch):
    """Control: without the inline marker, an image-bearing tool row keeps
    its existing parts-list replay (view_image/MCP behavior unchanged)."""
    import asyncio

    from backend.agent.imagedata import save_bytes
    from backend.agent.loop import load_history
    from backend.db.database import add_message, create_conversation

    cid = asyncio.run(create_conversation("viewimg-control", None))
    rel = save_bytes(PNG_BYTES, "png", "viewtest")
    asyncio.run(
        add_message(
            cid,
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "call_v1",
                    "type": "function",
                    "function": {"name": "view_image", "arguments": "{}"},
                }
            ],
        )
    )
    asyncio.run(
        add_message(cid, "tool", json.dumps({"note": "fetched"}), tool_call_id="call_v1", images=[rel])
    )
    msgs = asyncio.run(load_history(cid))
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert isinstance(tool_msgs[0]["content"], list)
    assert any(p.get("type") == "image_url" for p in tool_msgs[0]["content"])


def test_generated_image_marker_survives_persist_roundtrip(configured, tmp_path, monkeypatch):
    """display:'inline' reaches get_messages() rows - the UI renders from
    persisted state, not just the live stream."""
    row = _persist_generate_roundtrip(configured, tmp_path, monkeypatch)
    assert json.loads(row["content"])["display"] == "inline"


# --------------------------------------------------------- settings probe


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    monkeypatch.setattr(mainmod, "save_config", cfgmod.save_config)
    from backend.main import app

    with TestClient(app) as c:
        yield c


def test_probe_rejects_bad_input(client):
    r = client.post("/api/image-provider/probe", json={"api_base": ""})
    assert r.status_code == 200 and r.json()["ok"] is False
    r = client.post("/api/image-provider/probe", json={"api_base": "ftp://x"})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_config_put_blank_unconfigures_after_configure(client):
    """Blank endpoint after a configured block: the stored api_base must
    actually clear (regression: popping the update kept the old block)."""
    r = client.put(
        "/api/config", json={"image_generation": {"api_base": "http://127.0.0.1:1234"}}
    )
    assert r.status_code == 200
    r = client.put("/api/config", json={"image_generation": {"api_base": ""}})
    assert r.status_code == 200
    got = client.get("/api/config").json()["image_generation"]
    assert got["api_base"] == ""


def test_config_get_exposes_block(configured, client):
    got = client.get("/api/config").json()
    assert got["image_generation"]["api_base"] == "http://127.0.0.1:1234"
    assert got["image_generation"]["provider"] == "sdcpp"


# --------------------------------------------------- cancellable job transport


def test_failed_job_reports_server_error(configured, tmp_path, monkeypatch):
    """A failed native job surfaces the server's message, not a hang."""
    _FakeClient.caps_payload = {"upscale": True}
    _FakeClient.job_status = "failed"
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert "failed: boom" in result["error"]


def test_transient_poll_errors_do_not_fail_the_job(configured, tmp_path, monkeypatch):
    """CodeRabbit major: ONE dropped poll (15s timeout, 503, malformed
    body) must not fail a job that is still running server-side - the
    wait loop retries until its deadline and the image still lands."""
    _FakeClient.caps_payload = {"upscale": True}
    _FakeClient.poll_queue = [
        {"status": "generating", "result": None, "error": None},  # ok
        503,                                        # server hiccup
        {"status": "queued", "result": None, "error": None},
        {"status": "generating", "result": None, "error": None},
        "MALFORMED",                                # non-JSON body
        {"status": "generating", "result": None, "error": None},
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image", {"prompt": "x", "width": 512, "height": 512},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    assert result["width"] == 512 and result["height"] == 512
    assert _FakeClient.cancel_calls == 0


def test_gone_job_is_still_terminal(configured, tmp_path, monkeypatch):
    """404/410 (TTL purge / cancelled server-side) stays terminal even
    with retry-on-transient in place - no waiting out a dead job."""
    _FakeClient.caps_payload = {"upscale": True}
    _FakeClient.poll_queue = [404]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert "expired server-side" in result["error"]


def test_malformed_submit_body_falls_back_to_legacy(configured, tmp_path, monkeypatch):
    """CodeRabbit minor: a truthy non-mapping 200 submit body must not
    raise AttributeError out of generate_image - it degrades to the
    sync fallback like any other native-path failure."""
    _FakeClient.caps_payload = {"upscale": True}
    _FakeClient.submit_status = 200
    _FakeClient.submit_payload = ["not", "a", "mapping"]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert not result.get("error"), result
    assert _FakeClient.gen_calls == 1  # legacy endpoint was used


def test_stop_cancels_the_job_and_reraises(configured, tmp_path, monkeypatch):
    """The reason the job transport exists: Stop (CancelledError) must
    fire a best-effort server-side cancel and re-raise immediately, so
    the harness never waits out a thrashing server (rc.14 incident)."""
    import asyncio as aio

    _FakeClient.caps_payload = {"upscale": True}
    _FakeClient.job_status = "generating"  # never completes
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    async def run_and_cancel():
        task = aio.create_task(
            tools.execute_tool(
                "generate_image", {"prompt": "x"}, workspace=str(tmp_path)
            )
        )
        await aio.sleep(0.05)  # let it submit + enter the poll loop
        task.cancel()
        try:
            await task
            return None
        except aio.CancelledError:
            return "cancelled"

    outcome = asyncio.run(run_and_cancel())
    assert outcome == "cancelled"
    assert _FakeClient.cancel_calls == 1


def test_legacy_server_without_native_api_uses_sync_fallback(configured, tmp_path, monkeypatch):
    """404 on /sdcpp/v1/img_gen (older sd-server build): fall back to the
    sync OpenAI-compatible endpoint - generation still works, just not
    cancellable (the old behavior)."""
    _FakeClient.native_ok = False
    _FakeClient.caps_payload = {"upscale": True}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert not result.get("error"), result
    assert _FakeClient.gen_calls == 1
    # the omit path's built-in 1024x1024 is the NATIVE render size
    # (#421); the mid-fallback legacy size string carries those render
    # dims (what the diffusion pass actually sees)
    assert _FakeClient.gen_sizes == ["1024x1024"]
    assert _FakeClient.job_bodies == []


def test_native_body_carries_native_fields(configured, tmp_path, monkeypatch):
    """negative_prompt/seed go as native body fields on the job path (no
    sd_cpp_extra_args embedding - that's a compatibility-API mechanism)."""
    _FakeClient.caps_payload = {"upscale": True}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {"prompt": "x", "negative_prompt": "dogs", "seed": 42},
            workspace=str(tmp_path),
        )
    )
    assert not result.get("error"), result
    body = _FakeClient.job_bodies[0]
    assert body["negative_prompt"] == "dogs"
    assert body["seed"] == 42
    assert "sd_cpp_extra_args" not in body["prompt"]
