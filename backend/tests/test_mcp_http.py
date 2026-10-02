"""Issue #128: remote MCP servers over HTTP (streamable HTTP with legacy
SSE fallback), env interpolation, backoff and version surfacing."""
import asyncio
import threading
import time

import pytest
import uvicorn

from backend.agent import mcp_client


# ------------------------------------------------------------------ config

def test_url_entry_is_accepted_and_started(monkeypatch):
    started = []

    class FakeManager(mcp_client.McpManager):
        def _launch(self, state):
            started.append(state.name)

    monkeypatch.setattr(mcp_client, "manager", None, raising=False)
    mgr = FakeManager()
    mgr.servers = {}
    monkeypatch.setattr(mcp_client.McpManager, "configured",
                        lambda self: {"remote": {"url": "https://mcp.example.com/mcp"}})
    mgr.start_all()
    assert started == ["remote"]
    assert mgr.servers["remote"].spec["url"] == "https://mcp.example.com/mcp"


def test_env_interpolation():
    import os
    monkeypatch_env = {"MCP_TOKEN": "sekrit"}
    spec = mcp_client.interpolate_env(
        {"url": "https://x/mcp", "headers": {"Authorization": "Bearer ${env:MCP_TOKEN}"}},
        monkeypatch_env.get,
    )
    assert spec["headers"]["Authorization"] == "Bearer sekrit"
    # unknown vars become empty, matching shell semantics for this use
    assert mcp_client.interpolate_env({"headers": {"k": "${env:NOPE}"}, "command": "x",
                                       "args": ["${env:MISSING}"], "env": {"a": "${env:MCP_TOKEN}"}},
                                      monkeypatch_env.get) == {
        "headers": {"k": ""}, "command": "x", "args": [""], "env": {"a": "sekrit"}}


# ------------------------------------------------------------------ backoff

def test_backoff_grows_and_gives_up(monkeypatch):
    st = mcp_client.McpServerState("x", {"command": "missing-cmd-xyz"})
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(mcp_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(mcp_client, "RESTART_BACKOFF", 2.0)
    # simulate 8 consecutive failures synchronously
    for _ in range(8):
        delay = mcp_client.next_backoff(st.failures)
        st.failures += 1
    assert [mcp_client.next_backoff(n) for n in range(0, 8)] == [
        2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]
    # give-up threshold: MAX_RESTARTS failures then terminal state
    assert mcp_client.MAX_RESTARTS == 8
    assert st.failures == 8
    # state flags terminal after threshold
    st.failures = mcp_client.MAX_RESTARTS
    assert st.will_stop_retrying()


def test_terminal_state_status_label():
    st = mcp_client.McpServerState("x", {"command": "c"})
    st.status = "failed"
    assert st.api_status() == "failed"
    st.failures = mcp_client.MAX_RESTARTS
    assert st.api_status() == "failed (won't retry)"


# ------------------------------------------------- session loop behavior

class _BoomCM:
    """Transport stand-in whose enter always fails (bad command, refused
    socket, 404-with-no-retry — the failure shape is irrelevant here)."""

    def __init__(self):
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        raise RuntimeError("boom: connect failed")

    async def __aexit__(self, *exc):
        return False


async def test_session_loop_backoff_then_terminal(monkeypatch):
    """Unchanged failing spec: sleep delays grow (5s, 10s, 20s, capped 30s),
    every failure bumps the counter, and after MAX_RESTARTS consecutive
    failures the loop RETURNS (terminal) instead of retrying forever."""
    cm = _BoomCM()
    monkeypatch.setattr(mcp_client.McpManager, "_open_transport", lambda self, st: cm)
    st = mcp_client.McpServerState("bad", {"url": "http://x/mcp"})
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)  # record and return: the retry loop spins fast

    # NOTE: patches the shared asyncio module (monkeypatch restores it);
    # instant sleep is exactly what makes the 8-attempt march fast.
    monkeypatch.setattr(mcp_client.asyncio, "sleep", fake_sleep)

    await asyncio.wait_for(
        mcp_client.McpManager._session_loop(mcp_client.manager, st), 10.0
    )
    assert sleeps[:4] == [5.0, 10.0, 20.0, 30.0]
    assert st.failures == mcp_client.MAX_RESTARTS
    assert st.status == "failed" and st.error
    assert st.api_status() == "failed (won't retry)"


# ------------------------------------------------- HTTP integration (real)

@pytest.fixture
def http_server():
    """Real MCP server over streamable HTTP + legacy SSE on 127.0.0.1."""
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("httptest")

    @mcp.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return f"echo: {text}"

    app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()
    app.mount("/legacy", sse_app)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    host, port = server.servers[0].sockets[0].getsockname()[:2]
    yield f"http://{host}:{port}"
    server.should_exit = True
    t.join(timeout=5)


async def test_streamable_http_roundtrip(http_server):
    st = mcp_client.McpServerState("rmt", {"url": f"{http_server}/mcp"})
    mcp_client.manager.servers["rmt"] = st
    st._task = asyncio.create_task(mcp_client.manager._session_loop(st))
    try:
        for _ in range(60):
            await asyncio.sleep(0.25)
            if st.status != "starting":
                break
        assert st.status == "connected", st.error
        assert [t["function"]["name"] for t in st.tools] == ["mcp_rmt_echo"]
        assert await mcp_client.manager.call("mcp_rmt_echo", {"text": "hi"}) == {"result": "echo: hi"}
        # negotiated protocol version is surfaced
        assert st.protocol_version
    finally:
        await mcp_client.manager.shutdown()


async def test_legacy_sse_fallback(http_server):
    st = mcp_client.McpServerState("old", {"url": f"{http_server}/legacy/sse", "transport": "sse"})
    mcp_client.manager.servers["old"] = st
    st._task = asyncio.create_task(mcp_client.manager._session_loop(st))
    try:
        for _ in range(60):
            await asyncio.sleep(0.25)
            if st.status != "starting":
                break
        assert st.status == "connected", st.error
        assert await mcp_client.manager.call("mcp_old_echo", {"text": "yo"}) == {"result": "echo: yo"}
    finally:
        await mcp_client.manager.shutdown()


async def test_url_with_headers_reaches_server(http_server):
    """Headers from config are applied; ${env:} interpolation covered above."""
    st = mcp_client.McpServerState(
        "hdr", {"url": f"{http_server}/mcp", "headers": {"X-Test": "1"}})
    mcp_client.manager.servers["hdr"] = st
    st._task = asyncio.create_task(mcp_client.manager._session_loop(st))
    try:
        for _ in range(60):
            await asyncio.sleep(0.25)
            if st.status != "starting":
                break
        assert st.status == "connected", st.error
    finally:
        await mcp_client.manager.shutdown()
