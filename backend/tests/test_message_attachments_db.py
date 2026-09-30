"""attachments column on messages (#142): migration + round-trip."""
import json
from pathlib import Path

import pytest

from backend.db.database import add_message, create_conversation, get_messages, init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    from backend.db import database

    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", Path(path))
    return path


async def _conversation() -> int:
    conv = await create_conversation("test", None)
    return conv


async def test_add_and_get_message_with_attachments(db):
    conv = await _conversation()
    records = [{"name": "a.txt", "size": 3, "content": "abc"}]
    mid = await add_message(conv, "user", "hello", attachments=records)
    rows = await get_messages(conv)
    row = next(r for r in rows if r["id"] == mid)
    assert row["attachments"] == records


async def test_messages_without_attachments_default_to_none(db):
    conv = await _conversation()
    await add_message(conv, "user", "hello")
    rows = await get_messages(conv)
    assert rows[0]["attachments"] is None


async def test_migration_adds_column_to_existing_database(tmp_path, monkeypatch):
    """An upgraded database keeps its rows and gains the attachments column."""
    import aiosqlite

    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as raw:
        # A pre-#142 database: messages table without attachments.
        await raw.execute(
            "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " title TEXT NOT NULL DEFAULT '', workspace TEXT,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        await raw.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,"
            " role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',"
            " tool_calls TEXT, tool_call_id TEXT, images TEXT,"
            " sub_agent_transcript TEXT,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        await raw.execute(
            "INSERT INTO conversations (title) VALUES ('old chat')"
        )
        await raw.execute(
            "INSERT INTO messages (conversation_id, role, content)"
            " VALUES (1, 'user', 'legacy text')"
        )
        await raw.commit()
    from backend.db import database
    monkeypatch.setattr(database, "DB_PATH", path)
    await init_db()
    async with aiosqlite.connect(path) as raw:
        cur = await raw.execute("SELECT * FROM messages")
        cols = {d[0] for d in cur.description}
        rows = await (await raw.execute("SELECT id, content FROM messages")).fetchall()
    assert "attachments" in cols
    assert rows == [(1, "legacy text")]  # no data loss
