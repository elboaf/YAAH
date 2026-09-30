"""Structured text attachments end-to-end at the API/loop seams (#142)."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.agent.attachments import reinline_attachments


@pytest.fixture
def db(tmp_path, monkeypatch):
    from backend.db import database

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _conversation() -> int:
    from backend.db.database import create_conversation

    return await create_conversation("t", None)


async def test_send_persists_structured_attachments(db):
    conv = await _conversation()
    records = [{"name": "notes.md", "size": 8, "content": "# hello\n"}]
    async with await _client() as client:
        res = await client.post(
            f"/api/agent/{conv}",
            json={
                "message": "please review",
                "workspace": ".",
                "attachments": records,
            },
        )
    # The endpoint streams; it may error on the model call, but the user row
    # with the attachments must already be persisted.
    from backend.db.database import get_messages

    rows = await get_messages(conv)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert any(
        r["content"] == "please review" and r["attachments"] == records
        for r in user_rows
    ), [(r["content"], r["attachments"]) for r in user_rows]


async def test_queue_accepts_attachments_and_echoes_them(db):
    import backend.agent.loop as loop

    conv = await _conversation()
    loop._running_convs.add(conv)  # pretend a run is in flight
    try:
        records = [{"name": "a.txt", "size": 3, "content": "abc"}]
        async with await _client() as client:
            res = await client.post(
                f"/api/agent/{conv}/queue",
                json={"message": "also look at this", "attachments": records},
            )
        assert res.status_code == 200
        items = loop.queue_items(conv)
        assert items[0]["attachments"] == records
    finally:
        loop._running_convs.discard(conv)


async def test_injected_queued_attachments_are_persisted(db, monkeypatch):
    """At the boundary, the queued item's attachments ride into the user row."""
    import backend.agent.loop as loop

    conv = await _conversation()
    loop._message_queues.pop(conv, None)  # queue is in-memory, shared across tests
    records = [{"name": "big.log", "size": 204_800, "path": ".yaah-attachments/big.log"}]
    loop.enqueue_message(conv, "check this", [], [], records)

    # Run against a fake agent that ends the stream immediately.
    async def fake_stream(*a, **k):
        if False:
            yield ""

    monkeypatch.setattr(loop, "_call_model", fake_stream, raising=False)
    # Drive _take_injections directly: it is the boundary seam.
    items = await loop._take_injections(conv)
    assert items[0]["attachments"] == records
    from backend.db.database import get_messages

    rows = await get_messages(conv)
    assert rows[-1]["attachments"] == records
    assert rows[-1]["content"] == "check this"


async def test_load_history_reinlines_byte_identically(db):
    from backend.db.database import add_message, get_messages

    conv = await _conversation()
    records = [
        {"name": "notes.md", "size": 8, "content": "# hello\n"},
        {"name": "big.log", "size": 204_800, "path": ".yaah-attachments/big.log"},
    ]
    await add_message(conv, "user", "please review", attachments=records)
    history = await loop_load_history(conv)
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "please review" + "".join(
        reinline_attachment_expected(r) for r in records
    )


async def loop_load_history(conv):
    from backend.agent.loop import load_history

    return await load_history(conv)


def reinline_attachment_expected(r):
    from backend.agent.attachments import inline_attachment_text

    return inline_attachment_text(r)


async def test_legacy_rows_without_attachments_replay_unchanged(db):
    from backend.db.database import add_message

    conv = await _conversation()
    await add_message(conv, "user", "plain text")
    history = await loop_load_history(conv)
    assert history[0]["content"] == "plain text"
