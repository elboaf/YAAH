"""MCP client tests: a real stdio server (backend/tests/mcp_fixtures/
demo_server.py) driven through the manager — the same path production
uses. No mock: if the mcp SDK changes its wire protocol or attribute
names, these fail loudly."""
import asyncio
import sys

import pytest

from backend.agent import mcp_client


@pytest.fixture
async def demo_server():
    """A connected McpServerState against the fixture server."""
    st = mcp_client.McpServerState(
        "demo", {"command": sys.executable, "args": ["-u", "backend/tests/mcp_fixtures/demo_server.py"]}
    )
    mcp_client.manager.servers["demo"] = st
    st._task = asyncio.create_task(mcp_client.manager._session_loop(st))
    if not await st.wait_connected(15.0):
        await mcp_client.manager.shutdown()
        pytest.fail(f"demo server failed to connect: {st.error}")
    yield st
    await mcp_client.manager.shutdown()


async def test_discovery_and_prefixed_names(demo_server):
    names = [t["function"]["name"] for t in demo_server.tools]
    assert names == ["mcp_demo_echo", "mcp_demo_add"]
    # schemas are OpenAI-shaped with a real parameters object
    assert demo_server.tools[0]["function"]["parameters"]["type"] == "object"


async def test_call_roundtrip(demo_server):
    assert await mcp_client.manager.call("mcp_demo_add", {"a": 2, "b": 3}) == {"result": "5"}
    assert await mcp_client.manager.call("mcp_demo_echo", {"text": "hi"}) == {"result": "echo: hi"}


async def test_call_errors_are_dicts(demo_server):
    res = await mcp_client.manager.call("mcp_demo_nope", {})
    assert "error" in res
    # unknown server prefix
    res = await mcp_client.manager.call("mcp_ghost_tool", {})
    assert "unavailable" in res["error"]


async def test_image_blocks_use_v2_mime_attr(demo_server, monkeypatch):
    """Regression: mcp 2.x renamed ImageContent.mimeType -> mime_type; the
    old hard attribute read crashed the turn when a server returned an
    image (puppeteer_screenshot). Either name must work now."""
    import base64

    import backend.agent.imagedata as imagedata

    monkeypatch.setattr(
        imagedata, "save_bytes", lambda raw, ext, sub: f"{sub}/fake.{ext}"
    )

    class Block:
        type = "image"
        data = base64.b64encode(b"\x89PNG-fake").decode()
        mime_type = "image/png"

    class Result:
        content = [Block()]
        is_error = False

    class FakeSession:
        async def call_tool(self, name, arguments):
            return Result()

    demo_server._session = FakeSession()
    res = await mcp_client.manager.call("mcp_demo_screenshot", {})
    assert res["images"] == ["mcp/fake.png"]


async def test_schemas_merge_and_routing(demo_server):
    from backend.agent.tools import execute_tool, get_schemas

    names = {s["function"]["name"] for s in get_schemas()}
    assert "mcp_demo_add" in names
    # execute_tool routes mcp_ names before the static executor map
    res = await execute_tool("mcp_demo_add", {"a": 20, "b": 22}, "ws")
    assert res["result"] == "42"


async def test_find_splits_known_prefixes(demo_server):
    state, tool = mcp_client.manager.find("mcp_demo_echo")
    assert state is demo_server and tool == "echo"
    state, tool = mcp_client.manager.find("no_prefix_at_all")
    assert state is None and tool == ""


def test_name_validation_via_api():
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:  # lifespan starts the (empty) manager
        r = client.post("/api/mcp/servers", json={"name": "bad name!", "command": "x"})
        assert r.status_code == 400
        r = client.post("/api/mcp/servers", json={"name": "ok-name", "command": ""})
        assert r.status_code == 400
        servers = client.get("/api/mcp/servers").json()["servers"]
        assert isinstance(servers, list)


def test_url_server_upsert_and_status_payload(monkeypatch):
    """Issue #128: url entries register like command entries (exactly one
    of command/url), headers/env are stored raw (${env:} unresolved) and
    the status payload exposes url/headers/env/protocol_version."""
    from fastapi.testclient import TestClient

    from backend.main import app

    started = []

    def fake_start_all(self):
        started.extend(self.servers.keys())

    monkeypatch.setattr(mcp_client.McpManager, "start_all", fake_start_all)
    cfg: dict = {}

    def fake_configured(self):
        return cfg

    def fake_save(d):
        # copy first: the payload aliases the live dict returned by
        # configured(), and clearing before reading would wipe it
        entries = dict(d.get("mcpServers") or {})
        cfg.clear()
        cfg.update(entries)

    monkeypatch.setattr(mcp_client.McpManager, "configured", fake_configured)
    monkeypatch.setattr("backend.main._save_config", fake_save)

    with TestClient(app) as client:
        # neither -> 400
        r = client.post("/api/mcp/servers", json={"name": "nourl"})
        assert r.status_code == 400
        # both -> 400
        r = client.post(
            "/api/mcp/servers", json={"name": "both", "command": "x", "url": "https://a/mcp"}
        )
        assert r.status_code == 400
        # bad scheme -> 400
        r = client.post("/api/mcp/servers", json={"name": "ftp", "url": "ftp://a/mcp"})
        assert r.status_code == 400
        # valid remote entry
        r = client.post(
            "/api/mcp/servers",
            json={
                "name": "remote1",
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "Bearer ${env:MCP_TOKEN}"},
                "env": {"A": "1"},
            },
        )
        assert r.status_code == 200, r.text
        row = next(s for s in r.json()["servers"] if s["name"] == "remote1")
        assert row["url"] == "https://mcp.example.com/mcp"
        assert row["headers"]["Authorization"] == "Bearer ${env:MCP_TOKEN}"  # raw, not resolved
        assert row["env"] == {"A": "1"}
        # upsert: same name again updates (edit path). #129 merge semantics:
        # hand-added keys in config.json survive a UI edit, so headers here
        # survive even though the edit body omits them; the changed field
        # still propagates.
        r = client.post(
            "/api/mcp/servers",
            json={"name": "remote1", "url": "https://other.example.com/mcp"},
        )
        assert r.status_code == 200
        row = next(s for s in r.json()["servers"] if s["name"] == "remote1")
        assert row["url"] == "https://other.example.com/mcp"
        assert row["headers"]["Authorization"] == "Bearer ${env:MCP_TOKEN}"
        # local entry still works
        r = client.post("/api/mcp/servers", json={"name": "loc1", "command": "demo-cmd", "args": ["a"]})
        assert r.status_code == 200
        row = next(s for s in r.json()["servers"] if s["name"] == "loc1")
        assert row["command"] == "demo-cmd" and row["args"] == ["a"] and row["url"] == ""
