"""Tests for the DB layer and API routes."""
import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.db.database import add_message, create_conversation, get_messages


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
