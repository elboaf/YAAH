"""Issue #11: "workspaces cant be deleted, sometimes?"

The user's sequence: work in a workspace, then remove it. Its chats relocate
to Default, but the workspace row reappears in the sidebar (immediately or
after an app restart), and removing it again "does nothing" until restart.

Mechanism: every agent turn / workspace switch persists the path to
config.json (`last_workspace`), and migrate_workspaces() — which runs on
EVERY get_db() — re-seeds the registry from config.json's last_workspace.
Nothing cleared last_workspace on delete, so the very next API call
resurrected the deleted row.

The suite shares one database file (conftest redirects YAAH_DB_PATH once
per session), so these tests reset the DB, the config file, and the
once-per-process seed flag before each test: the resurrection path must be
exercised from a clean registry regardless of where the suite ran before.
"""

import pytest
from httpx import ASGITransport, AsyncClient

import backend.main as main
import backend.db.database as database
from backend.agent.config import CONFIG_PATH, load_config
from backend.db.database import list_conversations, list_workspaces


@pytest.fixture(autouse=True)
def fresh_db_and_config():
    """Empty registry + empty config + unconsumed seed flag, per test."""
    import os

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(database.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    CONFIG_PATH.unlink(missing_ok=True)
    # Keep the suite-wide contract (conftest): the access-mode gate stays
    # open; nothing here executes tools, but load_config() consumers see
    # the same defaults the rest of the suite runs with.
    CONFIG_PATH.write_text('{"access_mode": "full"}', encoding="utf-8")
    database._last_workspace_seeded = False
    yield


@pytest.fixture()
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as c:
        yield c


async def test_delete_last_active_workspace_resurrects(client, tmp_path):
    """Delete the workspace that is config.json's last_workspace -> the next
    API call (the sidebar refresh) resurrected it. This is the reported bug."""
    # tmp_path: POST /api/workspaces makedirs()es missing paths, so a hardcoded
    # path would create real directories outside the test sandbox.
    victim = str(tmp_path / "Victim")
    r = await client.post("/api/workspaces", json={"path": victim})
    assert r.status_code == 200, r.text
    ws = r.json()  # path comes back resolved; use it, never the raw input

    # The user works there: a conversation is filed in the workspace and the
    # UI remembers it as the last workspace (the switch/turn-start path).
    r = await client.post(
        "/api/conversations", json={"title": "task", "workspace": ws["path"]}
    )
    assert r.status_code == 200, r.text
    conv_id = r.json()["id"]
    r = await client.post(
        "/api/config/last-workspace", json={"workspace": ws["path"]}
    )
    assert r.status_code == 200, r.text

    # Remove it (what the user does from the sidebar).
    r = await client.delete(f"/api/workspaces/{ws['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["relocated"] == 1

    # Its chat relocated to Default...
    convs = await list_conversations()
    mine = [c for c in convs if c["id"] == conv_id]
    assert mine and mine[0]["workspace"] is None

    # ...but the next sidebar refresh (any API call) resurrects the row:
    rows = (await client.get("/api/workspaces")).json()
    assert ws["path"] not in [r["path"] for r in rows], (
        "deleted workspace came back — migrate_workspaces() re-seeds it from "
        "config.json last_workspace"
    )


async def test_delete_clears_last_workspace_so_restart_stays_clean(client, tmp_path):
    """Regression for issue #11: removing the last-active workspace must clear
    config.json's last_workspace (the registry re-seed source), otherwise the
    deleted row resurrects on the next API call and after every restart."""
    victim = str(tmp_path / "Victim")
    r = await client.post("/api/workspaces", json={"path": victim})
    assert r.status_code == 200, r.text
    ws = r.json()
    r = await client.post("/api/config/last-workspace", json={"workspace": ws["path"]})
    assert r.status_code == 200, r.text
    r = await client.delete(f"/api/workspaces/{ws['id']}")
    assert r.status_code == 200, r.text

    # The delete forgot the stale last_workspace...
    assert load_config().get("last_workspace") != ws["path"]

    # ...so a restart (fresh process: config load + first DB access re-running
    # the migration) does NOT resurrect the deleted row.
    database._last_workspace_seeded = False  # simulate the fresh process
    paths = [r["path"] for r in await list_workspaces()]
    assert ws["path"] not in paths
