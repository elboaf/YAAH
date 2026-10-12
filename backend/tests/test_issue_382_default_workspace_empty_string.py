"""Issue #382: Default-workspace chats were saved with workspace='' and
vanished from the sidebar.

The sidebar groups chats under Default by matching the registry row's NULL
path; a row stored as '' matched no group (its messages stayed in the DB,
unreachable from the UI). The legacy per-request migrate healed such rows
silently on every API call; since f6b05a4 (#374) that pass runs once per
process, so every workspace WRITE must enforce the NULL contract itself.

Non-git workspaces are unaffected and fully supported: added folders store
their real path and group fine — only the Default pseudo-workspace (no
root directory) is represented by NULL.

Run: pytest backend/tests/test_issue_382_default_workspace_empty_string.py
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

import backend.main as main
import backend.db.database as database
from backend.agent.config import CONFIG_PATH
from backend.db.database import list_conversations, list_workspaces, normalize_workspace


@pytest.fixture(autouse=True)
def fresh_db_and_config():
    """Empty registry + config + fresh schema pass, per test (see #11/#374)."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(database.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    CONFIG_PATH.unlink(missing_ok=True)
    # Keep the suite-wide contract (conftest): the access-mode gate stays
    # open; nothing here executes tools.
    CONFIG_PATH.write_text('{"access_mode": "full"}', encoding="utf-8")
    database._last_workspace_seeded = False
    database._migrated.discard(str(database.DB_PATH))
    yield


@pytest.fixture()
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as c:
        yield c


def test_normalize_workspace_contract():
    assert normalize_workspace("") is None
    assert normalize_workspace("  ") is None
    assert normalize_workspace(".") is None
    assert normalize_workspace(None) is None
    assert normalize_workspace(" C:\\proj ") == "C:\\proj"
    assert normalize_workspace("remote:abc:") == "remote:abc:"


async def test_create_chat_with_empty_string_files_under_default(client):
    """The reported bug: the frontend's draft pin ships '' for Default; the
    row must land NULL (Default group), not '' (no group at all)."""
    r = await client.post(
        "/api/conversations",
        json={"title": "default chat", "workspace": "", "model": "", "effort": ""},
    )
    assert r.status_code == 200
    cid = r.json()["id"]
    convs = {c["id"]: c for c in await list_conversations()}
    assert convs[cid]["workspace"] is None
    rows = {w["path"]: w for w in await list_workspaces()}
    assert rows[None]["conversation_count"] == 1, "chat must count under Default"


async def test_move_chat_to_default_lands_null(client, tmp_path):
    """Moving a chat to Default ('' from the client) must not poison the row
    the turn endpoint derives its working directory from."""
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    await client.post("/api/workspaces", json={"path": str(ws_dir)})
    r = await client.post(
        "/api/conversations",
        json={"title": "t", "workspace": str(ws_dir), "model": "", "effort": ""},
    )
    cid = r.json()["id"]
    r = await client.post(f"/api/conversations/{cid}/move", json={"workspace": ""})
    assert r.status_code == 200
    assert r.json()["workspace"] is None
    convs = {c["id"]: c for c in await list_conversations()}
    assert convs[cid]["workspace"] is None


async def test_patch_chat_workspace_empty_string_lands_null(client, tmp_path):
    ws_dir = tmp_path / "ws2"
    ws_dir.mkdir()
    r = await client.post(
        "/api/conversations",
        json={"title": "t", "workspace": str(ws_dir), "model": "", "effort": ""},
    )
    cid = r.json()["id"]
    r = await client.patch(f"/api/conversations/{cid}", json={"workspace": ""})
    assert r.status_code == 200
    convs = {c["id"]: c for c in await list_conversations()}
    assert convs[cid]["workspace"] is None


async def test_real_workspace_paths_pass_through(client, tmp_path):
    """The normalizer is for ''/'.' only: real folders keep their path."""
    ws = str(tmp_path / "real")
    r = await client.post(
        "/api/conversations",
        json={"title": "t", "workspace": ws, "model": "", "effort": ""},
    )
    cid = r.json()["id"]
    convs = {c["id"]: c for c in await list_conversations()}
    assert convs[cid]["workspace"] == str(ws)  # resolve()d by the layer below
