"""Image generation via a stable-diffusion.cpp server (sd-server).

Covers the v1 design decided in grill: a dedicated `image_generation`
config block (never a chat providers entry), a generate_image tool that
appears only while api_base is configured (memory/sandbox gating
posture), OpenAI-compatible /v1/images/generations transport with
negative_prompt/seed riding the sd_cpp_extra_args block, a workspace
copy under ai-images/ plus the inline-display store rel, local-only
refusal for remote workspaces, and main-chat-only visibility
(sub-agents never see the tool).
"""
import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agent import config as cfgmod
from backend.agent import subagents, tools


PNG_BYTES = b"\x89PNG-fake-image"


class _FakeResp:
    status_code = 200

    def json(self):
        return {"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]}


class _FakeClient:
    last = None

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.last = (url, json)
        return _FakeResp()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Isolated config with an sd-server configured."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    cfgmod.save_config(
        {
            "image_generation": {
                "provider": "sdcpp",
                "api_base": "http://127.0.0.1:1234",
                "size": "512x512",
            }
        }
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
    """A save touching only size must not wipe api_base (API-level block
    merge, same rule as the remote/memory blocks; save_config itself is a
    shallow update by design)."""
    r = client.put(
        "/api/config",
        json={"image_generation": {"api_base": "http://127.0.0.1:1234"}},
    )
    assert r.status_code == 200
    r = client.put("/api/config", json={"image_generation": {"size": "768x768"}})
    assert r.status_code == 200
    block = client.get("/api/config").json()["image_generation"]
    assert block["api_base"] == "http://127.0.0.1:1234"
    assert block["size"] == "768x768"


# ------------------------------------------------------------ tool gating


def test_schema_hidden_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    assert "generate_image" not in _schema_names()


def test_schema_present_when_configured(configured):
    assert "generate_image" in _schema_names()


def test_execute_tool_refused_when_unconfigured(tmp_path, monkeypatch):
    """Memory/sandbox posture: config vanished mid-run -> refuse, point at
    Settings, name no config keys."""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    result = asyncio.run(
        tools.execute_tool("generate_image", {"prompt": "x"}, workspace=str(tmp_path))
    )
    assert "Settings" in result["info"]
    assert "api_base" not in result["info"]


def test_sub_agents_never_get_generate_image(configured):
    """Main-chat-only: even configured, the tool is excluded from every
    sub-agent's resolved schema."""
    defn = subagents.AgentDef(name="x", description="", body="", tools=None)
    resolved = subagents._resolve_tools(defn, workspace=None)
    assert "generate_image" not in [s["function"]["name"] for s in resolved]


# --------------------------------------------------------------- executor


def test_generate_image_end_to_end(configured, tmp_path, monkeypatch):
    """One call: OpenAI-compat request with embedded extra args, store rel
    for inline display, workspace copy under ai-images/."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = asyncio.run(
        tools.execute_tool(
            "generate_image",
            {
                "prompt": "a cat",
                "negative_prompt": "dogs",
                "seed": 42,
                "width": 256,
                "height": 256,
            },
            workspace=str(ws),
        )
    )
    url, body = _FakeClient.last
    assert url == "http://127.0.0.1:1234/v1/images/generations"
    assert body["n"] == 1
    assert body["size"] == "256x256"
    assert "sd_cpp_extra_args" in body["prompt"]
    embedded = json.loads(
        body["prompt"].split("<sd_cpp_extra_args>")[1].split("</sd_cpp_extra_args>")[0]
    )
    assert embedded == {"negative_prompt": "dogs", "seed": 42}
    assert result["images"] and result["images"][0].startswith("generate_image/")
    assert result["path"].startswith("ai-images/")
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
        tools.execute_tool(
            "generate_image", {"prompt": "x"}, workspace=str(tmp_path)
        )
    )
    assert "HTTP 500" in result["error"]


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


def test_config_put_strips_blank_api_base(client):
    """Blank endpoint = unconfigure: no api_base comes back in the GET view."""
    r = client.put(
        "/api/config",
        json={"image_generation": {"api_base": "", "size": "512x512"}},
    )
    assert r.status_code == 200
    got = client.get("/api/config").json()
    assert not (got.get("image_generation") or {}).get("api_base")


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
