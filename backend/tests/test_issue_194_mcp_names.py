"""Issue #194: MCP server-name validation, exact routing, description
clamp. Unit-level (no live server) — routing uses fabricated states whose
.tools lists mimic what _discover would store."""
from backend.agent import mcp_client


def _state(name, advertised):
    st = mcp_client.McpServerState(name, {"command": "x"})
    st.status = "connected"
    st.tools = [
        {"type": "function", "function": {"name": f"mcp_{name}_{t}", "description": "", "parameters": {}}}
        for t in advertised
    ]
    return st


# --------------------------------------------------------- name validation

def test_validate_server_name_accepts_normal_names():
    for n in ("browser", "a_b", "My-Server2", "a" * 40):
        assert mcp_client.validate_server_name(n) is None


def test_validate_server_name_rejects_bad():
    assert mcp_client.validate_server_name("") is not None
    assert mcp_client.validate_server_name(None) is not None
    assert mcp_client.validate_server_name("   ") is not None
    assert mcp_client.validate_server_name("_") is not None
    assert mcp_client.validate_server_name("mcp_x") is not None
    assert mcp_client.validate_server_name("mcp") is not None


def test_start_all_skips_invalid_names(monkeypatch):
    calls = []

    monkeypatch.setattr(
        mcp_client.McpManager, "configured",
        lambda self: {"mcp_evil": {"command": "bash"}, "ok": {"command": "bash"}},
    )
    monkeypatch.setattr(mcp_client.McpManager, "_launch", lambda self, s: calls.append(s.name))
    m = mcp_client.McpManager()
    m.start_all()
    assert "mcp_evil" not in m.servers
    assert calls == ["ok"]


# ---------------------------------------------------------------- routing

def test_find_routes_advertised_owner_over_longer_prefix():
    m = mcp_client.McpManager()
    m.servers["a"] = _state("a", ["b_c"])       # advertises mcp_a_b_c
    m.servers["a_b"] = _state("a_b", [])        # advertises nothing
    # Legacy first-match would route to 'a_b' tool 'c', which never existed.
    state, tool = m.find("mcp_a_b_c")
    assert state.name == "a" and tool == "b_c"


def test_find_prefers_advertised_owner_on_ambiguity():
    m = mcp_client.McpManager()
    m.servers["a"] = _state("a", ["b_c"])       # advertises mcp_a_b_c
    m.servers["a_b"] = _state("a_b", ["c"])     # advertises mcp_a_b_c too
    state, tool = m.find("mcp_a_b_c")
    # Both parses fit; the longest (most specific) advertised owner wins.
    assert state.name == "a_b" and tool == "c"


def test_find_falls_back_to_longest_prefix_when_unadvertised():
    m = mcp_client.McpManager()
    m.servers["a"] = _state("a", [])
    m.servers["a_b"] = _state("a_b", [])
    state, tool = m.find("mcp_a_b_thing")
    assert state.name == "a_b" and tool == "thing"


def test_find_still_rejects_no_prefix():
    m = mcp_client.McpManager()
    m.servers["demo"] = _state("demo", ["add"])
    assert m.find("bash") == (None, "")


# ------------------------------------------------------------ description clamp

def test_clamp_description_caps_length():
    long = "x" * 10_000
    clamped = mcp_client.clamp_description(long)
    assert len(clamped) <= mcp_client._MAX_DESC_CHARS + len("…[clamped]")
    assert clamped.startswith("x" * 10)
    assert mcp_client.clamp_description("short") == "short"
    assert mcp_client.clamp_description(None) == ""
