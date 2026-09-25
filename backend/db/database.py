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
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    remote_revision_counter INTEGER NOT NULL DEFAULT 1,
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

-- Read-only cache of conversations owned by connected remote devices.
-- Remote integer IDs are only unique within their host, never locally.
CREATE TABLE IF NOT EXISTS remote_conversations (
    host_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT 'New Task',
    workspace TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    revision TEXT NOT NULL DEFAULT '',
    sync_status TEXT NOT NULL DEFAULT 'synced',
    PRIMARY KEY (host_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS remote_messages (
    host_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (host_id, conversation_id, message_id),
    FOREIGN KEY (host_id, conversation_id)
        REFERENCES remote_conversations(host_id, conversation_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_remote_messages_order
    ON remote_messages(host_id, conversation_id, position);

-- Host-enforced edit leases and idempotent remote conversation commits.
-- Expirations use Unix seconds so they can be atomically compared in SQLite.
CREATE TABLE IF NOT EXISTS remote_edit_leases (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    lease_token TEXT NOT NULL,
    holder_id TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_conversation_commits (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    commit_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, commit_id)
);

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
    if "prompt_summary" not in conv_cols:
        await db.execute("ALTER TABLE conversations ADD COLUMN prompt_summary TEXT")
    if "prompt_summary_through_message_id" not in conv_cols:
        await db.execute(
            "ALTER TABLE conversations ADD COLUMN prompt_summary_through_message_id INTEGER"
        )
    if "chat_type" not in conv_cols:
        # 'chat' = a normal conversation; 'agent' = a scheduled agent's pinned
        # chat (issue #41). Existing rows are normal chats by default.
        await db.execute(
            "ALTER TABLE conversations ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'chat'"
        )
    if "model" not in conv_cols or "effort" not in conv_cols:
        # #51/#76: per-chat model + reasoning effort, mirroring the agents
        # columns. NOT NULL DEFAULT '' = follow the global default — the
        # pre-per-chat behavior for existing rows.
        if "model" not in conv_cols:
            await db.execute(
                "ALTER TABLE conversations ADD COLUMN model TEXT NOT NULL DEFAULT ''"
            )
        if "effort" not in conv_cols:
            await db.execute(
                "ALTER TABLE conversations ADD COLUMN effort TEXT NOT NULL DEFAULT ''"
            )
        await _stamp_conversation_scopes(db)
    if "remote_revision_counter" not in conv_cols:
        # Revision 1 is a stable baseline token for conversations created
        # before the remote-edit protocol existed.
        await db.execute(
            "ALTER TABLE conversations ADD COLUMN remote_revision_counter INTEGER NOT NULL DEFAULT 1"
        )
    cur = await db.execute("PRAGMA table_info(agents)")
    agent_cols = {r[1] for r in await cur.fetchall()}
    if "allow_ask_user" not in agent_cols:
        # #93: per-agent opt-in letting a scheduled run block on ask_user.
        # Default 0 preserves the unattended contract for existing agents.
        await db.execute("ALTER TABLE agents ADD COLUMN allow_ask_user INTEGER NOT NULL DEFAULT 0")
    # Revision tokens change for every host-side metadata/transcript edit,
    # including legacy local routes, not just commits through the new API.
    await db.executescript("""
    CREATE TRIGGER IF NOT EXISTS conversations_remote_revision_update
    AFTER UPDATE OF title, workspace, system_prompt_override, model, effort
    ON conversations
    BEGIN
        UPDATE conversations SET remote_revision_counter = remote_revision_counter + 1
        WHERE id = NEW.id;
    END;
    CREATE TRIGGER IF NOT EXISTS messages_remote_revision_insert
    AFTER INSERT ON messages BEGIN
        UPDATE conversations SET remote_revision_counter = remote_revision_counter + 1
        WHERE id = NEW.conversation_id;
    END;
    CREATE TRIGGER IF NOT EXISTS messages_remote_revision_update
    AFTER UPDATE OF role, content, tool_calls, tool_call_id, images, sub_agent_transcript
    ON messages BEGIN
        UPDATE conversations SET remote_revision_counter = remote_revision_counter + 1
        WHERE id = NEW.conversation_id;
    END;
    CREATE TRIGGER IF NOT EXISTS messages_remote_revision_delete
    AFTER DELETE ON messages BEGIN
        UPDATE conversations SET remote_revision_counter = remote_revision_counter + 1
        WHERE id = OLD.conversation_id;
    END;
    """)
    await migrate_workspaces(db)
    return db


_stamp_scope_done = False


async def _stamp_conversation_scopes(db: aiosqlite.Connection):
    """One-time stamp of every conversations row with the then-current global
    model + effort (issue #51/#76 "inherit = stamp on upgrade").

    Runs only on the upgrade that ADDS the columns: from then on every row
    carries its own explicit value, and `''` in the wild means the chat's
    owner set Default deliberately (never "inherit whatever the global is
    right now"). Agent-pinned chats are skipped — their selectors write
    through to the owning agent, so the agent's model/effort IS the chat's.
    Idempotent via the module flag (get_db runs on every API call).
    """
    global _stamp_scope_done
    if _stamp_scope_done:
        return
    _stamp_scope_done = True
    from backend.agent.config import load_config

    cfg = load_config()
    global_model = cfg.get("model") or ""
    global_effort = cfg.get("reasoning_effort") or ""
    await db.execute(
        "UPDATE conversations SET model = ?, effort = ?"
        " WHERE chat_type != 'agent' AND (model = '' OR effort = '')",
        (global_model, global_effort),
    )
    # Agent-pinned chats resolve through their agent at send time; stamp the
    # rows anyway so the header can display the agent's values directly and
    # the resolve helper never sees a blank it might mistake for a choice.
    await db.execute(
        "UPDATE conversations SET model = '', effort = '' WHERE chat_type = 'agent'"
    )
    await db.commit()


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
    title: str = "New Task",
    workspace: str | None = None,
    chat_type: str = "chat",
    model: str | None = None,
    effort: str | None = None,
):
    """Create a conversation. model/effort: the chat's pinned scope (#51/#76).
    Blank strings are legal writes (the chat's deliberate Default); None
    stamps the current global default (drafts already carry explicit picks,
    so None is the "unspecified" path for API callers)."""
    if model is None or effort is None:
        from backend.agent.config import load_config

        cfg = load_config()
        if model is None:
            model = cfg.get("model") or ""
        if effort is None:
            effort = cfg.get("reasoning_effort") or ""
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO conversations (title, workspace, chat_type, model, effort)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, workspace, chat_type, model, effort),
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
        rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            row.pop("prompt_summary", None)
            row.pop("prompt_summary_through_message_id", None)
        return rows
    finally:
        await db.close()


async def get_conversation(conversation_id: int) -> dict | None:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        row = await cur.fetchone()
        result = dict(row) if row else None
        if result:
            result.pop("prompt_summary", None)
            result.pop("prompt_summary_through_message_id", None)
        return result
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
    """Update allowed conversation fields (title, workspace,
    system_prompt_override, model, effort)."""
    allowed = {"title", "workspace", "system_prompt_override", "model", "effort"}
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
    conversation_id: int, summary: str, through_message_id: int
) -> int:
    """Persist prompt-only compaction without changing transcript rows.

    The prompt summary is a bounded cumulative summary through a message
    watermark. User-facing reads and exports continue to see every original
    row. A concurrent append does not affect the boundary; a missing or
    stale watermark is rejected. Returns the count of transcript messages
    covered by the summary, or zero when the update could not be applied.
    """
    db = await get_db()
    try:
        await db.execute("BEGIN")
        cur = await db.execute(
            "SELECT prompt_summary_through_message_id FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        conv = await cur.fetchone()
        if conv is None:
            await db.rollback()
            return 0
        previous_id = conv["prompt_summary_through_message_id"] or 0
        if through_message_id <= previous_id:
            await db.rollback()
            return 0
        cur = await db.execute(
            "SELECT COUNT(*) AS count, MAX(id) AS max_id FROM messages"
            " WHERE conversation_id = ? AND id > ? AND id <= ?",
            (conversation_id, previous_id, through_message_id),
        )
        boundary = await cur.fetchone()
        count = boundary["count"]
        if not count or boundary["max_id"] != through_message_id:
            await db.rollback()
            return 0
        cur = await db.execute(
            "UPDATE conversations SET prompt_summary = ?,"
            " prompt_summary_through_message_id = ?,"
            " context_tokens = NULL, context_model = NULL"
            " WHERE id = ? AND COALESCE(prompt_summary_through_message_id, 0) = ?",
            (summary, through_message_id, conversation_id, previous_id),
        )
        if cur.rowcount != 1:
            await db.rollback()
            return 0
        await db.commit()
        return count
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


async def upsert_remote_conversation(host_id: str, conversation: dict, messages: list[dict]) -> None:
    """Replace one cached remote transcript atomically; IDs are host-scoped."""
    cid = str(conversation["id"])
    db = await get_db()
    try:
        await db.execute("BEGIN")
        await db.execute(
            """INSERT INTO remote_conversations
               (host_id, conversation_id, title, workspace, updated_at, revision, sync_status)
               VALUES (?, ?, ?, ?, ?, ?, 'synced')
               ON CONFLICT(host_id, conversation_id) DO UPDATE SET
                 title=excluded.title, workspace=excluded.workspace,
                 updated_at=excluded.updated_at, revision=excluded.revision,
                 sync_status='synced'""",
            (host_id, cid, conversation.get("title", "New Task"),
             conversation.get("workspace"), conversation.get("updated_at", ""),
             str(conversation.get("revision", ""))),
        )
        await db.execute(
            "DELETE FROM remote_messages WHERE host_id=? AND conversation_id=?",
            (host_id, cid),
        )
        for position, message in enumerate(messages):
            await db.execute(
                "INSERT INTO remote_messages(host_id, conversation_id, message_id, position, payload) VALUES (?, ?, ?, ?, ?)",
                (host_id, cid, str(message.get("id", position)), position,
                 json.dumps(message)),
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def list_remote_conversations(host_id: str) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT host_id, conversation_id, title, workspace, updated_at, revision, sync_status FROM remote_conversations WHERE host_id=? ORDER BY updated_at DESC",
            (host_id,),
        )
        return [dict(row) for row in await cur.fetchall()]
    finally:
        await db.close()


class RemoteProtocolError(Exception):
    """A lease/revision conflict with a machine-readable protocol code."""

    def __init__(self, code: str, **details):
        self.code = code
        self.details = details
        super().__init__(code)


def _revision_token(counter: int) -> str:
    return f"r:{counter}"


async def acquire_remote_edit_lease(
    conversation_id: int, revision: str, holder_id: str = ""
) -> dict:
    """Atomically check the revision and acquire a bounded host-side lease."""
    import secrets
    import time

    lease_seconds = 120
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT remote_revision_counter FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        conversation = await cur.fetchone()
        if conversation is None:
            await db.rollback()
            raise RemoteProtocolError("conversation_not_found")
        current_revision = _revision_token(conversation["remote_revision_counter"])
        if revision != current_revision:
            await db.rollback()
            raise RemoteProtocolError(
                "stale_revision", current_revision=current_revision,
                provided_revision=revision,
            )
        now = int(time.time())
        cur = await db.execute(
            "SELECT holder_id, expires_at FROM remote_edit_leases"
            " WHERE conversation_id = ? AND expires_at > ?",
            (conversation_id, now),
        )
        active = await cur.fetchone()
        if active is not None:
            await db.rollback()
            raise RemoteProtocolError(
                "lease_held", holder_id=active["holder_id"],
                expires_at=active["expires_at"],
            )
        token = secrets.token_urlsafe(32)
        expires_at = now + lease_seconds
        await db.execute(
            "INSERT INTO remote_edit_leases(conversation_id, lease_token, holder_id, expires_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET"
            " lease_token=excluded.lease_token, holder_id=excluded.holder_id,"
            " expires_at=excluded.expires_at",
            (conversation_id, token, holder_id, expires_at),
        )
        await db.commit()
        return {"lease_token": token, "expires_at": expires_at,
                "revision": current_revision, "lease_seconds": lease_seconds}
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def renew_remote_edit_lease(conversation_id: int, lease_token: str) -> dict:
    import time

    lease_seconds = 120
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        cur = await db.execute(
            "SELECT expires_at FROM remote_edit_leases"
            " WHERE conversation_id = ? AND lease_token = ? AND expires_at > ?",
            (conversation_id, lease_token, now),
        )
        if await cur.fetchone() is None:
            await db.rollback()
            raise RemoteProtocolError("lease_invalid_or_expired")
        expires_at = now + lease_seconds
        await db.execute(
            "UPDATE remote_edit_leases SET expires_at = ? WHERE conversation_id = ?",
            (expires_at, conversation_id),
        )
        await db.commit()
        return {"lease_token": lease_token, "expires_at": expires_at,
                "lease_seconds": lease_seconds}
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def release_remote_edit_lease(conversation_id: int, lease_token: str) -> bool:
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT lease_token, expires_at FROM remote_edit_leases WHERE conversation_id = ?",
            (conversation_id,),
        )
        lease = await cur.fetchone()
        if lease is None:
            await db.commit()
            return False
        import time
        if lease["lease_token"] != lease_token or lease["expires_at"] <= int(time.time()):
            await db.rollback()
            raise RemoteProtocolError("lease_invalid_or_expired")
        await db.execute(
            "DELETE FROM remote_edit_leases WHERE conversation_id = ?",
            (conversation_id,),
        )
        await db.commit()
        return True
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def get_remote_conversation_snapshot(conversation_id: int) -> dict | None:
    db = await get_db()
    try:
        await db.execute("BEGIN")
        cur = await db.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        conversation = dict(row)
        conversation["revision"] = _revision_token(
            conversation.pop("remote_revision_counter")
        )
        conversation.pop("prompt_summary", None)
        conversation.pop("prompt_summary_through_message_id", None)
        cur = await db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
        messages = [dict(item) for item in await cur.fetchall()]
        for message in messages:
            for field in ("tool_calls", "images", "sub_agent_transcript"):
                raw = message.get(field)
                message[field] = json.loads(raw) if raw else ([] if field == "images" else None)
        return {"conversation": conversation, "messages": messages,
                "revision": conversation["revision"]}
    finally:
        await db.close()


async def commit_remote_conversation(
    conversation_id: int,
    *,
    lease_token: str,
    revision: str,
    commit_id: str,
    conversation: dict,
    messages: list[dict],
) -> dict:
    """Validate lease+revision and atomically replace a snapshot exactly once."""
    import hashlib
    import time

    request = {"revision": revision, "conversation": conversation, "messages": messages}
    request_hash = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT request_hash, response_json FROM remote_conversation_commits"
            " WHERE conversation_id = ? AND commit_id = ?",
            (conversation_id, commit_id),
        )
        previous = await cur.fetchone()
        if previous is not None:
            if previous["request_hash"] != request_hash:
                await db.rollback()
                raise RemoteProtocolError("commit_id_reused")
            result = json.loads(previous["response_json"])
            result["replayed"] = True
            await db.commit()
            return result

        cur = await db.execute(
            "SELECT remote_revision_counter FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        conv = await cur.fetchone()
        if conv is None:
            await db.rollback()
            raise RemoteProtocolError("conversation_not_found")
        now = int(time.time())
        cur = await db.execute(
            "SELECT lease_token, expires_at FROM remote_edit_leases"
            " WHERE conversation_id = ?",
            (conversation_id,),
        )
        active = await cur.fetchone()
        if active is None or active["lease_token"] != lease_token or active["expires_at"] <= now:
            await db.rollback()
            raise RemoteProtocolError("lease_invalid_or_expired")
        current_revision = _revision_token(conv["remote_revision_counter"])
        if revision != current_revision:
            await db.rollback()
            raise RemoteProtocolError(
                "stale_revision", current_revision=current_revision,
                provided_revision=revision,
            )

        fields = {field: conversation[field] for field in ("title", "workspace") if field in conversation}
        if fields:
            sets = ", ".join(f"{field} = ?" for field in fields)
            await db.execute(
                f"UPDATE conversations SET {sets}, updated_at = datetime('now') WHERE id = ?",
                (*fields.values(), conversation_id),
            )
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        normalized_ids = []
        for message in messages:
            try:
                candidate = int(message.get("id"))
                if candidate <= 0 or str(candidate) != str(message.get("id")):
                    raise ValueError
                normalized_ids.append(candidate)
            except (TypeError, ValueError):
                normalized_ids.append(None)
        keep_ids = (
            all(mid is not None for mid in normalized_ids)
            and normalized_ids == sorted(set(normalized_ids))
        )
        if keep_ids and normalized_ids:
            placeholders = ",".join("?" for _ in normalized_ids)
            cur = await db.execute(
                f"SELECT 1 FROM messages WHERE id IN ({placeholders}) LIMIT 1",
                normalized_ids,
            )
            # IDs colliding with another conversation cannot be preserved;
            # let SQLite allocate host-local IDs instead of failing the commit.
            keep_ids = await cur.fetchone() is None
        for index, message in enumerate(messages):
            role = message.get("role")
            if role not in ("user", "assistant", "tool", "system"):
                await db.rollback()
                raise RemoteProtocolError("invalid_message_role", index=index)
            columns = "conversation_id, role, content, tool_calls, tool_call_id, images, sub_agent_transcript"
            values = [
                conversation_id, role, message.get("content", ""),
                json.dumps(message["tool_calls"]) if message.get("tool_calls") is not None else None,
                message.get("tool_call_id"),
                json.dumps(message["images"]) if message.get("images") else None,
                json.dumps(message["sub_agent_transcript"]) if message.get("sub_agent_transcript") else None,
            ]
            if keep_ids:
                columns = "id, " + columns
                values.insert(0, normalized_ids[index])
            if message.get("created_at"):
                columns += ", created_at"
                values.append(message["created_at"])
            placeholders = ", ".join("?" for _ in values)
            await db.execute(
                f"INSERT INTO messages ({columns}) VALUES ({placeholders})", values
            )
        await db.execute(
            "UPDATE conversations SET updated_at = datetime('now'),"
            " remote_revision_counter = remote_revision_counter + 1 WHERE id = ?",
            (conversation_id,),
        )
        cur = await db.execute(
            "SELECT remote_revision_counter FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        new_revision = _revision_token((await cur.fetchone())["remote_revision_counter"])
        result = {"ok": True, "commit_id": commit_id, "revision": new_revision,
                  "message_count": len(messages), "replayed": False}
        await db.execute(
            "INSERT INTO remote_conversation_commits"
            "(conversation_id, commit_id, request_hash, response_json) VALUES (?, ?, ?, ?)",
            (conversation_id, commit_id, request_hash,
             json.dumps(result, sort_keys=True, separators=(",", ":"))),
        )
        await db.commit()
        return result
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def get_remote_messages(host_id: str, conversation_id: str) -> list[dict] | None:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT payload FROM remote_messages WHERE host_id=? AND conversation_id=? ORDER BY position",
            (host_id, str(conversation_id)),
        )
        rows = await cur.fetchall()
        if not rows:
            cur = await db.execute(
                "SELECT 1 FROM remote_conversations WHERE host_id=? AND conversation_id=?",
                (host_id, str(conversation_id)),
            )
            return [] if await cur.fetchone() else None
        return [json.loads(row["payload"]) for row in rows]
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


async def get_prompt_summary(conversation_id: int) -> dict:
    """Return prompt-only compaction state, recognizing legacy summary rows.

    Old versions stored a summary as the first system row after deleting the
    transcript prefix. Those rows remain visible and searchable, but are
    treated as the initial prompt summary when no new-style summary exists.
    Failure markers are normally appended after user messages and are never
    mistaken for summaries.
    """
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT prompt_summary, prompt_summary_through_message_id"
            " FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        conv = await cur.fetchone()
        if conv is None:
            return {"summary": "", "through_message_id": 0}
        if conv["prompt_summary"]:
            return {
                "summary": conv["prompt_summary"],
                "through_message_id": conv["prompt_summary_through_message_id"] or 0,
            }
        cur = await db.execute(
            "SELECT id, role, content FROM messages WHERE conversation_id = ?"
            " ORDER BY id LIMIT 1",
            (conversation_id,),
        )
        legacy = await cur.fetchone()
        # Destructive legacy compaction placed its replacement system row at
        # the transcript's first id. Failure notices were appended after prior
        # user/assistant rows and must never become prompt summaries.
        if (
            legacy and legacy["role"] == "system"
            and not str(legacy["content"]).startswith("turn failed:")
        ):
            cur = await db.execute(
                "SELECT 1 FROM messages WHERE conversation_id = ? AND role = 'user'"
                " AND id > ? LIMIT 1",
                (conversation_id, legacy["id"]),
            )
            if await cur.fetchone():
                return {"summary": legacy["content"], "through_message_id": legacy["id"]}
        return {"summary": "", "through_message_id": 0}
    finally:
        await db.close()


async def search_conversation_history(
    conversation_id: int, query: str, max_results: int = 10
) -> dict:
    """Case-insensitive plain-text search over all textual transcript fields."""
    query = str(query or "").strip()
    if not query:
        return {"error": "query must not be empty"}
    try:
        max_results = max(1, min(int(max_results or 10), 50))
    except (TypeError, ValueError):
        max_results = 10
    needle = query.casefold()
    rows = await get_messages(conversation_id)
    state = await get_prompt_summary(conversation_id)
    candidates = []
    stopped = False
    for row in rows:
        fields = (
            ("content", row.get("content") or ""),
            ("tool_calls", json.dumps(row.get("tool_calls"), ensure_ascii=False) if row.get("tool_calls") else ""),
            ("tool_call_id", row.get("tool_call_id") or ""),
            ("sub_agent_transcript", json.dumps(row.get("sub_agent_transcript"), ensure_ascii=False) if row.get("sub_agent_transcript") else ""),
        )
        for field, value in fields:
            text = str(value)
            pos = text.casefold().find(needle)
            if pos >= 0:
                start, end = max(0, pos - 160), min(len(text), pos + len(query) + 160)
                excerpt = text[start:end]
                if start:
                    excerpt = "…" + excerpt
                if end < len(text):
                    excerpt += "…"
                candidates.append({
                    "message_id": row["id"],
                    "role": row["role"],
                    "field": field,
                    "excerpt": excerpt,
                })
                if len(candidates) > max_results:
                    stopped = True
                    break
        if stopped:
            break
    summary = state["summary"]
    summary_pos = summary.casefold().find(needle)
    if summary_pos >= 0:
        start, end = max(0, summary_pos - 160), min(len(summary), summary_pos + len(query) + 160)
        excerpt = summary[start:end]
        if start:
            excerpt = "…" + excerpt
        if end < len(summary):
            excerpt += "…"
        candidates.append({"message_id": None, "role": "prompt_summary", "field": "prompt_summary", "excerpt": excerpt})
        if len(candidates) > max_results:
            stopped = True
    return {
        "matches": candidates[:max_results],
        "count": len(candidates[:max_results]),
        "truncated": stopped,
    }

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


async def get_agent_for_conversation(conversation_id: int) -> dict | None:
    """The scheduled agent that owns a pinned chat, or None (#51/#76).

    Agent chats' model/effort resolve through their agent at send time and
    their header selectors write through to the agent, so the agent's
    values ARE the chat's."""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM agents WHERE conversation_id = ?", (conversation_id,)
        )
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
