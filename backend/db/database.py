"""SQLite persistence layer (hybrid schema).

conversations: one row per task.
messages: chat messages; tool calls stored as a JSON column on the row.
"""
import json
import os
import re
import sys
from pathlib import Path

import aiosqlite


def _default_data_dir() -> Path:
    """Data dir for the real database/config.

    Dev runs keep everything in the repo's backend/data/. The packaged app
    runs from a PyInstaller --onefile bundle, where __file__ points into a
    throwaway temp extraction that is deleted on exit — writing there meant
    every launch started from an empty database (user history loss,
    2026-09-11). Frozen builds persist under ~/.yaah instead.
    """
    if getattr(sys, "frozen", False):
        home = Path.home() / ".yaah"
        home.mkdir(parents=True, exist_ok=True)
        return home
    return Path(__file__).parent.parent / "data"


# YAAH_DB_PATH lets the test suite redirect this to a throwaway file —
# the default path is the user's real database.
DB_PATH = Path(
    os.environ.get("YAAH_DB_PATH")
    or _default_data_dir() / "agent.db"
)

# migrate_workspaces seeds config.json's last_workspace into the registry once
# per process (module global), not on every get_db() call — see the comment in
# migrate_workspaces.
_last_workspace_seeded = False


def basename(path: str) -> str:
    """Display label for a workspace path (its final segment).

    Splits on both separators: the registry may hold Windows-style paths
    while the app itself runs on POSIX (Path.name wouldn't split '\\').
    """
    parts = [p for p in re.split(r"[\\/]", path) if p]
    return parts[-1] if parts else path

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
    sub_agent_transcript TEXT, -- JSON snapshot of a sub-agent run (spawn_agent results)
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

-- Scheduled agents (issue #41): first-class recurring runs, one pinned
-- conversation each. schedule_spec is JSON interpreted per schedule_type:
-- interval -> {"minutes": N}; daily -> {"time": "HH:MM"};
-- weekly -> {"weekday": 0-6 (Mon=0), "time": "HH:MM"}.
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    schedule_type TEXT NOT NULL DEFAULT 'interval',
    schedule_spec TEXT NOT NULL DEFAULT '{}',
    approval_policy TEXT NOT NULL DEFAULT 'sandbox-only',
    model TEXT NOT NULL DEFAULT '',           -- '' = the active global model
    effort TEXT NOT NULL DEFAULT '',          -- '' = don't send reasoning_effort
    memory_enabled INTEGER NOT NULL DEFAULT 1,
    allow_ask_user INTEGER NOT NULL DEFAULT 0, -- #93: scheduled run may ask and wait
    retention INTEGER NOT NULL DEFAULT 0,     -- runs kept; 0 = unlimited
    notify_on_success INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    next_fire_at TEXT,                        -- ISO local; skipped when in the past on boot
    last_fired_at TEXT,
    last_finished_at TEXT,
    last_status TEXT,                         -- 'running' | 'ok' | 'error' (toast source)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Standing instructions: typed messages in an agent chat never trigger a
-- run; each becomes one row here, appended to the prompt at every fire.
CREATE TABLE IF NOT EXISTS agent_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
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
    if "sub_agent_transcript" not in cols:
        await db.execute("ALTER TABLE messages ADD COLUMN sub_agent_transcript TEXT")
    cur = await db.execute("PRAGMA table_info(conversations)")
    conv_cols = {r[1] for r in await cur.fetchall()}
    if "context_tokens" not in conv_cols:
        await db.execute("ALTER TABLE conversations ADD COLUMN context_tokens INTEGER")
    if "context_model" not in conv_cols:
        await db.execute("ALTER TABLE conversations ADD COLUMN context_model TEXT")
    if "chat_type" not in conv_cols:
        # 'chat' = a normal conversation; 'agent' = a scheduled agent's pinned
        # chat (issue #41). Existing rows are normal chats by default.
        await db.execute(
            "ALTER TABLE conversations ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'chat'"
        )
    cur = await db.execute("PRAGMA table_info(agents)")
    agent_cols = {r[1] for r in await cur.fetchall()}
    if "allow_ask_user" not in agent_cols:
        # #93: per-agent opt-in letting a scheduled run block on ask_user.
        # Default 0 preserves the unattended contract for existing agents.
        await db.execute("ALTER TABLE agents ADD COLUMN allow_ask_user INTEGER NOT NULL DEFAULT 0")
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
    # This seed is ONCE PER PROCESS: migrate_workspaces runs on every get_db
    # (i.e. every API call), and re-seeding per call resurrects a deleted
    # workspace whenever config.json still names it — the mechanism behind
    # issue #11. The durable case (restart) is covered by delete_workspace
    # clearing last_workspace when its path is removed.
    global _last_workspace_seeded
    if not _last_workspace_seeded:
        _last_workspace_seeded = True
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
        # config.json's last_workspace is a registry seed source (migrate_workspaces
        # re-inserts it on every get_db), so if it still names the deleted path the
        # very next API call resurrects the deleted row (issue #11). Forget it.
        # Case-folded compare: the registry dedupes paths case-insensitively
        # (upsert_workspace), so C:\Proj and c:\proj are the same workspace.
        from backend.agent.config import load_config, save_config

        try:
            last = (load_config().get("last_workspace") or "").strip()
            if last.casefold() == (ws["path"] or "").strip().casefold():
                save_config({"last_workspace": ""})
        except OSError:
            pass
        return {"relocated": relocated}
    finally:
        await db.close()


async def init_db():
    db = await get_db()
    await db.close()


# ---- Conversation CRUD ----

async def create_conversation(
    title: str = "New Task", workspace: str | None = None, chat_type: str = "chat"
):
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO conversations (title, workspace, chat_type) VALUES (?, ?, ?)",
            (title, workspace, chat_type),
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


async def move_conversation(conversation_id: int, target: str | None) -> dict | None:
    """Re-file a chat under another workspace (issue #8).

    The conversation row's `workspace` column is the single source of truth
    for where the chat LIVES; the turn endpoint derives each turn's working
    directory from it, so updating this row IS the move — the sidebar group
    and the cwd can never drift apart.

    target: the destination workspace path, or None for the Default
    pseudo-workspace (no root directory). Returns
    {"workspace": target-or-None, "target_id": registry id or None} —
    target_id is None when the destination is Default, which has no
    directory to register. None (chat unknown) means the caller returns 404.

    Renaming-on-move is deliberately out of scope here: if the target path
    doesn't exist on disk it is still registered (the sidebar already shows
    a ⚠ for dead workspaces), so the move is never silently lost.
    """
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
        )
        if await cur.fetchone() is None:
            return None
        target_id: int | None = None
        if target is not None:
            # Resolve/case-fold through the same dedupe the registry uses, so
            # 'C:/Proj' and 'c:\\proj\\' land on one row and one directory.
            row = await upsert_workspace(target)
            target = row["path"]
            target_id = row["id"]
        await db.execute(
            "UPDATE conversations SET workspace = ?,"
            " updated_at = datetime('now') WHERE id = ?",
            (target, conversation_id),
        )
        await db.commit()
        return {"workspace": target, "target_id": target_id}
    finally:
        await db.close()


async def set_conversation_usage(
    conversation_id: int, tokens: int, model: str | None
):
    """Record the context size (usage.prompt_tokens) of the latest model call
    in this conversation, plus the model id that produced it. Deliberately
    does NOT touch updated_at — a context readout must not re-sort the list."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE conversations SET context_tokens = ?, context_model = ? WHERE id = ?",
            (tokens, model, conversation_id),
        )
        await db.commit()
    finally:
        await db.close()


async def compact_conversation(
    conversation_id: int, summary: str, cut_messages: int
) -> int:
    """History compaction (adr/0004): atomically delete the oldest
    cut_messages rows and insert one system summary row in their place.

    The system row is skipped by load_history (model context) but the
    /messages read feeds it to the UI, which renders the divider. A
    row count smaller than cut_messages means the caller's rows are
    stale (something wrote concurrently) — the transaction is rolled
    back and 0 returned, so the caller reports "not compacted".

    Nulls context_tokens so the next model call re-measures the new,
    smaller context. Returns the number of rows actually removed.
    """
    db = await get_db()
    try:
        await db.execute("BEGIN")
        cur = await db.execute(
            "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id LIMIT ?",
            (conversation_id, cut_messages),
        )
        doomed = [r["id"] for r in await cur.fetchall()]
        if len(doomed) != cut_messages:
            await db.rollback()
            return 0
        # The summary row must SORT to where the prefix began: readers
        # order by id, so a natural insert would land at the transcript's
        # END. Delete first, then re-insert reusing the prefix's first id.
        first_id = doomed[0]
        await db.execute(
            f"DELETE FROM messages WHERE id IN ({','.join('?' * len(doomed))})",
            doomed,
        )
        await db.execute(
            "INSERT INTO messages (id, conversation_id, role, content)"
            " VALUES (?, ?, 'system', ?)",
            (first_id, conversation_id, summary),
        )
        await db.execute(
            "UPDATE conversations SET context_tokens = NULL, context_model = NULL"
            " WHERE id = ?",
            (conversation_id,),
        )
        await db.commit()
        return len(doomed)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def add_message(
    conversation_id: int,
    role: str,
    content: str,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
    images: list | None = None,
    sub_agent_transcript: dict | None = None,
):
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_calls,"
            " tool_call_id, images, sub_agent_transcript) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                json.dumps(images) if images else None,
                json.dumps(sub_agent_transcript) if sub_agent_transcript else None,
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
            # Transcript snapshots ride along for the UI but are NOT replayed
            # into model context (load_history never reads this column).
            r["sub_agent_transcript"] = (
                json.loads(r["sub_agent_transcript"])
                if r.get("sub_agent_transcript")
                else None
            )
        return rows
    finally:
        await db.close()

# ---- Scheduled agents (issue #41) ----

AGENT_FIELDS = (
    "workspace", "name", "prompt", "schedule_type", "schedule_spec",
    "approval_policy", "model", "effort", "memory_enabled",
    "allow_ask_user", "retention",
    "notify_on_success", "enabled", "conversation_id",
    "next_fire_at", "last_fired_at", "last_finished_at", "last_status",
)


async def create_agent(fields: dict) -> dict:
    """Insert one agent row; returns it as a dict. `id` may be supplied
    (tests) or omitted (the API layer generates one)."""
    allowed = {"id", *AGENT_FIELDS}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields.get("id"):
        import uuid

        fields["id"] = uuid.uuid4().hex[:12]
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    db = await get_db()
    try:
        await db.execute(
            f"INSERT INTO agents ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM agents WHERE id = ?", (fields["id"],))
        return dict(await cur.fetchone())
    finally:
        await db.close()


async def list_agents(workspace: str | None = None) -> list[dict]:
    db = await get_db()
    try:
        if workspace is None:
            cur = await db.execute("SELECT * FROM agents ORDER BY created_at")
        else:
            cur = await db.execute(
                "SELECT * FROM agents WHERE workspace = ? ORDER BY created_at",
                (workspace,),
            )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def get_agent(agent_id: str) -> dict | None:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def update_agent(agent_id: str, fields: dict) -> dict | None:
    """Merge-update allowed columns; bumps updated_at. Returns the fresh row."""
    fields = {k: v for k, v in fields.items() if k in AGENT_FIELDS}
    if not fields:
        return await get_agent(agent_id)
    sets = ", ".join(f"{k} = ?" for k in fields)
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE agents SET {sets}, updated_at = datetime('now') WHERE id = ?",
            (*fields.values(), agent_id),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def delete_agent(agent_id: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def list_instructions(agent_id: str) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM agent_instructions WHERE agent_id = ? ORDER BY id",
            (agent_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def add_instruction(agent_id: str, content: str) -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO agent_instructions (agent_id, content) VALUES (?, ?)",
            (agent_id, content),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT * FROM agent_instructions WHERE id = ?", (cur.lastrowid,)
        )
        return dict(await cur.fetchone())
    finally:
        await db.close()


async def update_instruction(instruction_id: int, content: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE agent_instructions SET content = ? WHERE id = ?",
            (content, instruction_id),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def delete_instruction(instruction_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            "DELETE FROM agent_instructions WHERE id = ?", (instruction_id,)
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def trim_agent_transcript(conversation_id: int, keep_runs: int):
    """Per-agent retention (issue #41): keep only the most recent `keep_runs`
    turns. A run starts at its (persisted) user message, so the transcript is
    cut just before the Nth-from-last user row; delete_message-by-range keeps
    tool/assistant rows attached to their run."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' "
            "ORDER BY id",
            (conversation_id,),
        )
        user_rows = [r[0] for r in await cur.fetchall()]
        if len(user_rows) <= keep_runs:
            return
        cutoff = user_rows[len(user_rows) - keep_runs]
        await db.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id < ?",
            (conversation_id, cutoff),
        )
        await db.commit()
    finally:
        await db.close()
