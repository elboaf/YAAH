"""Issue #8: "migrate chats between workspace" — allow chats to be moved
to other workspaces.

The trap this issue hides: a chat's sidebar grouping comes from the
conversation row's `workspace` column, but a turn's working directory
arrives separately (the client sends its active workspace on POST
/api/agent/{id}). A naive "move" that only re-filed the row would relabel
the chat under workspace B while every message kept running against
workspace A. The fix makes the row authoritative — the turn endpoint
derives the turn's workspace from it — plus a dedicated /move endpoint
that refuses while a run is streaming or messages sit queued (both were
written against the old workspace's context).

The suite shares one database file (conftest redirects YAAH_DB_PATH once
per session), so these tests reset the DB, the config file, and the
once-per-process seed flag before each test.
"""

import pytest
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

import backend.main as main
import backend.db.database as database
from backend.agent import loop as loop_mod
from backend.agent.config import CONFIG_PATH
from backend.db.database import get_conversation, list_workspaces


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
    loop_mod._message_queues.clear()
    yield
    loop_mod._message_queues.clear()


@pytest.fixture()
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as c:
        yield c


async def _new_chat(client, workspace=None, title="T") -> int:
    r = await client.post(
        "/api/conversations", json={"title": title, "workspace": workspace}
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_move_refiles_row_and_registers_target(client, tmp_path):
    """The row moves, and the destination joins the registry (the sidebar
    dropdown must list it without a manual add-workspace detour)."""
    dest = str(tmp_path / "Dest")
    cid = await _new_chat(client)
    r = await client.post(f"/api/conversations/{cid}/move", json={"workspace": dest})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["workspace"] == dest
    assert body["target_id"] is not None
    conv = await get_conversation(cid)
    assert conv["workspace"] == dest
    paths = {w["path"] for w in await list_workspaces()}
    assert dest in paths


async def test_move_to_default_sets_null_and_back(client, tmp_path):
    """Default (null) is a legal destination, and moving back to a real
    path re-files the row again — a chat can hop in both directions."""
    dest = str(tmp_path / "Dest")
    cid = await _new_chat(client, workspace=dest)
    r = await client.post(f"/api/conversations/{cid}/move", json={"workspace": None})
    assert r.status_code == 200
    assert r.json()["workspace"] is None
    assert r.json()["target_id"] is None
    assert (await get_conversation(cid))["workspace"] is None
    r = await client.post(f"/api/conversations/{cid}/move", json={"workspace": dest})
    assert r.status_code == 200
    assert (await get_conversation(cid))["workspace"] == dest


async def test_moved_chat_runs_next_turn_in_target_workspace(client, tmp_path):
    """The point of the issue: after a move, the chat's next turn streams
    against the TARGET workspace — even when the request still carries the
    old one. The conversation row is authoritative for the working
    directory; the sidebar grouping and the cwd cannot drift apart."""
    src = str(tmp_path / "Src")
    dest = str(tmp_path / "Dest")
    for p in (src, dest):
        r = await client.post("/api/workspaces", json={"path": p})
        assert r.status_code == 200, r.text
    cid = await _new_chat(client, workspace=src)
    assert (
        await client.post(f"/api/conversations/{cid}/move", json={"workspace": dest})
    ).status_code == 200

    seen = {}

    async def fake_run_agent(conversation_id, message, workspace, **kw):
        seen["workspace"] = workspace
        yield '{"type": "done"}'

    # The endpoint resolves run_agent from main's module namespace (imported
    # at module top), so that binding is the one to stub.
    real = main.run_agent
    main.run_agent = fake_run_agent
    try:
        body = main.AgentTurn(message="hi", workspace=src)  # stale on purpose
        resp = await main.api_agent_turn(cid, body)
        assert isinstance(resp, StreamingResponse)
        async for _ in resp.body_iterator:
            pass
    finally:
        main.run_agent = real
    assert seen["workspace"] == dest


async def test_move_refused_while_run_is_active(client, tmp_path):
    """A move mid-run would strand the in-flight turn's tools (already
    pointed at the old workspace) and its merge-back in the wrong tree."""
    dest = str(tmp_path / "Dest")
    cid = await _new_chat(client)
    real = loop_mod.agent_is_running
    loop_mod.agent_is_running = lambda cid: True
    try:
        r = await client.post(f"/api/conversations/{cid}/move", json={"workspace": dest})
    finally:
        loop_mod.agent_is_running = real
    assert r.status_code == 409
    assert (await get_conversation(cid))["workspace"] is None


async def test_move_refused_with_queued_messages(client, tmp_path):
    """Queued messages were written against the old workspace's context;
    they must be sent or discarded before the chat can move."""
    dest = str(tmp_path / "Dest")
    cid = await _new_chat(client)
    loop_mod._message_queues[cid] = [{"id": 1, "text": "in flight", "skills": []}]
    r = await client.post(f"/api/conversations/{cid}/move", json={"workspace": dest})
    assert r.status_code == 409
    assert (await get_conversation(cid))["workspace"] is None


async def test_move_unknown_conversation_404(client, tmp_path):
    dest = str(tmp_path / "Dest")
    r = await client.post("/api/conversations/99999/move", json={"workspace": dest})
    assert r.status_code == 404
