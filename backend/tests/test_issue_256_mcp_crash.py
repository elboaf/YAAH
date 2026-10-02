"""Issue #256: configuring an MCP server crashed the backend, and every
later navigation to the MCP server configuration page crashed it again.

The endpoints in backend/main.py consume the #128 client feature set
(``McpServerState.api_status``, ``protocol_version``, ``interpolate_env``,
URL-transport support), but master's backend/agent/mcp_client.py no longer
defined them — so both POST /api/mcp/servers (which returns the server list)
and GET /api/mcp/servers raised AttributeError. These tests pin the
endpoint contract through the real app, with config isolated per test.
"""

import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Real app, throwaway config: the MCP endpoints read the config file
    through backend.agent.config, so redirecting CONFIG_PATH is enough."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    from backend.main import app

    with TestClient(app) as c:
        yield c


def test_add_server_then_page_loads(client):
    """The user's exact chain: save a server, then load the page. Both
    must answer 200 with the server listed."""
    r = client.post(
        "/api/mcp/servers",
        json={
            "name": "demo",
            "command": sys.executable,
            "args": ["-u", "backend/tests/mcp_fixtures/demo_server.py"],
        },
    )
    assert r.status_code == 200, r.text
    names = [s["name"] for s in r.json()["servers"]]
    assert "demo" in names

    # Navigation to the configuration page right after the save.
    r = client.get("/api/mcp/servers")
    assert r.status_code == 200, r.text
    row = next(s for s in r.json()["servers"] if s["name"] == "demo")
    assert row["command"] == sys.executable
    assert isinstance(row["status"], str)


def test_page_loads_with_configured_but_unstarted_server(client, tmp_path):
    """A backend restart with a server already in config.json must not
    crash the first page visit: the row renders as 'starting' until the
    manager connects it."""
    from backend.agent import config as cfgmod

    cfgmod.save_config(
        {
            "mcpServers": {
                "ghost": {"command": "definitely-not-on-path-xyz", "args": []}
            }
        }
    )
    r = client.get("/api/mcp/servers")
    assert r.status_code == 200, r.text
    rows = {s["name"]: s for s in r.json()["servers"]}
    assert "ghost" in rows
    assert rows["ghost"]["status"] in ("starting", "failed")


def test_url_transport_roundtrip_keeps_uninterpolated_secret(client):
    """#128's contract: remote (url) servers are accepted, and re-saving
    an edit never persists resolved ${env:} values — the GET shows the
    raw template. The endpoint must not crash even though the URL is
    unreachable (connection failures belong in the row's error field)."""
    r = client.post(
        "/api/mcp/servers",
        json={
            "name": "remote",
            "url": "http://127.0.0.1:9/mcp",
            "headers": {"Authorization": "Bearer ${env:YAAH_NO_SUCH_TOKEN}"},
        },
    )
    assert r.status_code == 200, r.text
    row = next(s for s in r.json()["servers"] if s["name"] == "remote")
    assert row["url"] == "http://127.0.0.1:9/mcp"
    assert row["headers"]["Authorization"] == "Bearer ${env:YAAH_NO_SUCH_TOKEN}"
