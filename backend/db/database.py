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


def basename(path: str) -> str:
    """Display label for a workspace path (its final segment)."""
    p = Path(path)
    name = p.name or str(p)
    return name

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

CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL = the Default pseudo-workspace (no root directory). SQLite UNIQUE
    -- treats NULLs as distinct, so the Default guard lives in seed_default().
    path TEXT UNIQUE,
    label TEXT NOT NULL,
    last_opened_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
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
    await migrate_workspaces(db)
    return db


async def migrate_workspaces(db: aiosqlite.Connection):
    """One-time, idempotent migration to the workspace-organized model.

    - Legacy rows stored workspace as '' or '.' (the old DEFAULT_WORKSPACE);
      both mean "no root directory" and become NULL (the Default group).
    - The registry is seeded from the conversations that exist (label =
      basename, last_opened_at = the workspace's most recent activity) and
      from config.json's last_workspace, so the dropdown is populated on
      first launch after upgrading.
    """
    await db.execute(
        "UPDATE conversations SET workspace = NULL WHERE workspace IN ('', '.')"
    )
    cur = await db.execute("SELECT 1 FROM workspaces WHERE path IS NULL")
    if await cur.fetchone() is None:
        # UNIQUE treats NULLs as distinct, so the Default row is guarded here
        # rather than by INSERT OR IGNORE.
        await db.execute(
            "INSERT INTO workspaces (path, label, last_opened_at)"
            " VALUES (NULL, 'Default', datetime('now'))"
        )
    # SQLite has no basename(); compute labels in Python per distinct path.
    distinct = await db.execute(
        "SELECT workspace, MAX(updated_at) FROM conversations"
        " WHERE workspace IS NOT NULL GROUP BY workspace"
    )
    for path, updated in await distinct.fetchall():
        await db.execute(
            "INSERT OR IGNORE INTO workspaces (path, label, last_opened_at)"
            " VALUES (?, ?, ?)",
            (path, basename(path), updated),
        )
    # config.json's last_workspace (durable across a wiped DB) joins the
    # registry too, so the restored selection always exists in the dropdown.
    from backend.agent.config import load_config

    try:
        last = (load_config().get("last_workspace") or "").strip()
    except Exception:
        last = ""
    if last:
        await db.execute(
            "INSERT OR IGNORE INTO workspaces (path, label, last_opened_at) "
            "VALUES (?, ?, datetime('now'))",
            (last, basename(last)),
        )
    await db.commit()


async def list_workspaces() -> list[dict]:
    """Registry rows for the sidebar dropdown / grouped list.

    `exists` reflects the filesystem for real paths (dead paths stay listed
    but get a missing marker); Default always exists. `conversation_count`
    feeds the remove-workspace warning.
    """
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT w.id, w.path, w.label, w.last_opened_at,"
            " (SELECT COUNT(*) FROM conversations c"
            "  WHERE c.workspace IS w.path) AS conversation_count"
            " FROM workspaces w"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            if r["path"] is None:
                r["exists"] = True
            else:
                r["exists"] = os.path.isdir(r["path"])
        return rows
    finally:
        await db.close()


async def get_workspace_by_path(path: str | None) -> dict | None:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM workspaces WHERE path IS ?" if path is None
            else "SELECT * FROM workspaces WHERE path = ?",
            (path,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def upsert_workspace(path: str) -> dict:
    """Register a workspace (resolved, deduped) and touch its last_opened_at.

    Dedupe key is the resolved, case-folded path: Windows paths differing
    only in case or trailing slashes are the same folder.
    """
    resolved = str(Path(path).resolve())
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM workspaces")
        for row in await cur.fetchall():
            if row["path"] is not None and str(Path(row["path"]).resolve()).lower() == resolved.lower():
                await db.execute(
                    "UPDATE workspaces SET last_opened_at = datetime('now')"
                    " WHERE id = ?",
                    (row["id"],),
                )
                await db.commit()
                return dict(row) | {"last_opened_at": None}
        await db.execute(
            "INSERT INTO workspaces (path, label, last_opened_at)"
            " VALUES (?, ?, datetime('now'))",
            (resolved, basename(resolved)),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT * FROM workspaces WHERE path = ?", (resolved,)
        )
        return dict(await cur.fetchone())
    finally:
        await db.close()


async def touch_workspace(path: str | None):
    """Mark a workspace as just-used (switch or turn start)."""
    if path is None:
        return
    db = await get_db()
    try:
        await db.execute(
            "UPDATE workspaces SET last_opened_at = datetime('now')"
            " WHERE path = ?",
            (path,),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_workspace(workspace_id: int) -> dict:
    """Remove a workspace: its conversations relocate to Default (NULL).

    Returns {'relocated': n}. Raises ValueError for the Default row.
    """
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        row = await cur.fetchone()
        if row is None:
            raise KeyError(workspace_id)
        ws = dict(row)
        if ws["path"] is None:
            raise ValueError("the Default workspace cannot be removed")
        cur = await db.execute(
            "UPDATE conversations SET workspace = NULL WHERE workspace = ?",
            (ws["path"],),
        )
        relocated = cur.rowcount
        await db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        await db.commit()
        return {"relocated": relocated}
    finally:
        await db.close()


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