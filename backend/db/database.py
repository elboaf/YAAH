"""SQLite persistence layer (hybrid schema).

conversations: one row per task.
messages: chat messages; tool calls stored as a JSON column on the row.
"""
import json
import os
from pathlib import Path

import aiosqlite

# YAAH_DB_PATH lets the test suite redirect this to a throwaway file —
# the default path is the user's real database.
DB_PATH = Path(
    os.environ.get("YAAH_DB_PATH")
    or Path(__file__).parent.parent / "data" / "agent.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'New Task',
    workspace TEXT,
    system_prompt_override TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,          -- JSON array of OpenAI-format tool calls
    tool_call_id TEXT,        -- for role='tool' responses
    images TEXT,              -- JSON array of image rel paths (bytes on disk)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
"""


async def get_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    # WAL allows concurrent readers during writes; busy_timeout avoids
    # spurious "database is locked" errors under overlapping requests.
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 5000")
    # Idempotent: ensures schema exists even for direct calls outside app lifespan
    await db.executescript(SCHEMA)
    # Migrations: CREATE TABLE IF NOT EXISTS won't touch an existing table.
    cur = await db.execute("PRAGMA table_info(messages)")
    cols = {r[1] for r in await cur.fetchall()}
    if "images" not in cols:
        await db.execute("ALTER TABLE messages ADD COLUMN images TEXT")
    return db


async def init_db():
    db = await get_db()
    await db.close()


# ---- Conversation CRUD ----

async def create_conversation(title: str = "New Task", workspace: str | None = None):
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO conversations (title, workspace) VALUES (?, ?)",
            (title, workspace),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def list_conversations():
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def get_conversation(conversation_id: int) -> dict | None:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def delete_conversation(conversation_id: int) -> bool:
    """Delete a conversation and all its messages (FK cascade)."""
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def update_conversation(conversation_id: int, **fields):
    """Update allowed conversation fields (title, workspace, system_prompt_override)."""
    allowed = {"title", "workspace", "system_prompt_override"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    sets = ", ".join(f"{k} = ?" for k in updates)
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE conversations SET {sets},"
            " updated_at = datetime('now') WHERE id = ?",
            (*updates.values(), conversation_id),
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def add_message(
    conversation_id: int,
    role: str,
    content: str,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
    images: list | None = None,
):
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_calls,"
            " tool_call_id, images) VALUES (?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                json.dumps(images) if images else None,
            ),
        )
        await db.execute(
            "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
            (conversation_id,),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def get_messages(conversation_id: int):
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            if r["tool_calls"]:
                r["tool_calls"] = json.loads(r["tool_calls"])
            r["images"] = json.loads(r["images"]) if r.get("images") else []
        return rows
    finally:
        await db.close()