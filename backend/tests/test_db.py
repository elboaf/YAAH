"""Tests for the DB layer and API routes."""
import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.db.database import (
    add_message, create_conversation, get_messages,
    upsert_remote_conversation, list_remote_conversations, get_remote_messages,
) 


@pytest.mark.asyncio
async def test_conversation_and_message_roundtrip():
    cid = await create_conversation("Test Task", "/tmp/ws")
    await add_message(cid, "user", "hello")
    await add_message(
        cid,
        "assistant",
        "",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
    )
    msgs = await get_messages(cid)
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "bash"


@pytest.mark.asyncio
async def test_remote_cache_scopes_colliding_ids_by_host_and_preserves_order():
    local_id = await create_conversation("Local task")
    await add_message(local_id, "user", "local transcript")
    conversation = {"id": local_id, "title": "Remote task", "workspace": "C:/repo", "updated_at": "2025-01-02"}
    await upsert_remote_conversation("host-a", conversation, [
        {"id": 10, "role": "user", "content": "first"},
        {"id": 11, "role": "assistant", "content": "second"},
    ])
    await upsert_remote_conversation("host-b", {**conversation, "title": "Other"}, [
        {"id": 10, "role": "user", "content": "other host"},
    ])
    assert [m["content"] for m in await get_messages(local_id)] == ["local transcript"]
    assert (await list_remote_conversations("host-a"))[0]["title"] == "Remote task"
    assert [m["content"] for m in await get_remote_messages("host-a", str(local_id))] == ["first", "second"]
    assert [m["content"] for m in await get_remote_messages("host-b", str(local_id))] == ["other host"]
    assert await get_remote_messages("unknown-host", str(local_id)) is None


@pytest.mark.asyncio
async def test_health_endpoint(): 
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_conversation_api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/conversations", json={"title": "API Task"})
        assert r.status_code == 200
        cid = r.json()["id"]
        r = await client.post(
            f"/api/conversations/{cid}/messages",
            json={"role": "user", "content": "hi"},
        )
        assert r.status_code == 200
        r = await client.get(f"/api/conversations/{cid}/messages")
        assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_files_tree_and_preview_api(tmp_path):
    import httpx
    from backend.main import app

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("alpha\nbeta\n")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/files", params={"workspace": str(tmp_path)})
        assert r.status_code == 200
        tree = r.json()["tree"]
        assert tree[0]["name"] == "sub" and tree[0]["type"] == "dir"

        r = await c.post(
            "/api/files/preview",
            json={"workspace": str(tmp_path), "path": "sub/x.txt"},
        )
        assert r.status_code == 200
        assert "alpha" in r.json()["content"]


@pytest.mark.asyncio
async def test_export_markdown():
    import httpx
    from backend.db.database import add_message, create_conversation
    from backend.main import app

    cid = await create_conversation("Export Test")
    await add_message(cid, "user", "hello")
    await add_message(cid, "assistant", "doing thing", tool_calls=[
        {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    ])
    await add_message(cid, "tool", '{"ok": 1}', tool_call_id="c1")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/conversations/{cid}/export")
        assert r.status_code == 200
        body = r.text
        assert "# Export Test" in body
        assert "tool call: bash" in body
        assert "attachment" in r.headers["content-disposition"]


@pytest.mark.asyncio
async def test_conversation_update_system_prompt():
    import httpx
    from backend.db.database import create_conversation, get_conversation
    from backend.main import app

    cid = await create_conversation("cfg")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.patch(f"/api/conversations/{cid}", json={"system_prompt_override": "be terse"})
        assert r.json()["ok"] is True
    conv = await get_conversation(cid)
    assert conv["system_prompt_override"] == "be terse"
