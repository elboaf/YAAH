"""Background memory-extraction sub-agent (#341, spec #345).

Clean-room Python port of zcode's memory extraction (v3.14.3, Apache-2.0):
after a turn ends, a dedicated sub-agent reviews the exchange since a
durable cursor and maintains the memory store itself. Capture stops being
a by-product of mid-turn discretion; the charter does not change
(ADR-0009) - the extraction prompt replays the store's own when-to-save
text (memory._WHEN_TO_SAVE), so what may be saved is decided in exactly
one place.

Mechanics, mirroring zcode:
- durable per-conversation cursor (app_meta row): advanced on skip and on
  success, never on failure - failed content is retried next window;
- two skip gates before any model call (main agent already wrote memory;
  no genuine user prose) - both skips still advance the cursor;
- one extraction in flight, one pending slot: a newer snapshot replaces
  the pending one, so a fast conversation drains as one run per idle
  moment, never a queue;
- the writer is a confined sub-agent: its tool catalogue is exactly the
  three memory tools, which resolve slugs inside the memory root and
  cannot express a path outside it. It runs without the access-mode gate
  (auto-allowed writes - memory maintenance never nags), standalone with
  no event streaming; the saved memories are the visible artifact.

Runs only when memory is enabled (#169): extraction inherits that gate,
there is no second switch.
"""

import asyncio
import logging

from backend.agent import memory, subagents
from backend.agent.subagents import AgentDef
from backend.agent.tools import memory_workspace_for

log = logging.getLogger("yaah.extract")

# The writer's whole job is read-then-write over the memory store; five
# turns cover the two-pass strategy with slack, and the runner's budget
# nudge keeps a wanderer converging.
WRITER_MAX_TURNS = 5
# Manifest cap: the writer sees at most this many index lines (zcode caps
# at 200 as well).
MAX_MANIFEST_ENTRIES = 200
# A genuine user turn is at least this many words; tool-only and synthetic
# turns never trigger a model call.
MIN_PROSE_WORDS = 3
# The rendered window is tail-truncated to this many characters - recent
# content is what extraction is for.
MAX_WINDOW_CHARS = 24_000

# Nothing to save is zcode's convention: the writer's final message when
# the window holds nothing charter-worthy.
NOTHING_TO_SAVE = "Nothing to save."

WRITER_DEF = AgentDef(
    name="memory-extractor",
    description=(
        "Internal background memory maintainer - never spawned by the "
        "model; the turn-completion path runs it standalone."
    ),
    body=(
        "You are the background memory maintainer. You review a "
        "conversation window and keep the persistent memory store "
        "truthful: apply the retention test, update instead of "
        "duplicating, delete what turned out wrong, and stay silent "
        "(output only 'Nothing to save.') when nothing qualifies. Your "
        "only tools are the three memory tools; the window is your "
        "entire input - never investigate the repository."
    ),
    tools=["memory_save", "memory_read", "memory_delete"],
    max_turns=WRITER_MAX_TURNS,
    builtin=True,
)


# ------------------------------------------------------------------ cursor

def _cursor_key(conversation_id: int) -> str:
    return f"memory-extract-cursor:{conversation_id}"


async def _get_cursor(conversation_id: int) -> int:
    from backend.db.database import get_db

    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            (_cursor_key(conversation_id),),
        )
        row = await cur.fetchone()
        return int(row["value"]) if row else 0
    finally:
        await db.close()


async def _set_cursor(conversation_id: int, message_id: int) -> None:
    from backend.db.database import get_db

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_cursor_key(conversation_id), str(int(message_id))),
        )
        await db.commit()
    finally:
        await db.close()


# ------------------------------------------------------------------- gates

def _genuine_user_prose(window: list[dict]) -> bool:
    """True when the window holds a real user message of MIN_PROSE_WORDS+
    words. Synthetic turns (a scheduled fire's persisted prompt row is
    meta-tagged agent_prompt) and image-only messages don't count."""
    for m in window:
        if m.get("role") != "user":
            continue
        meta = m.get("meta") or {}
        if meta.get("agent_prompt"):
            continue
        text = str(m.get("content") or "").strip()
        if len(text.split()) >= MIN_PROSE_WORDS:
            return True
    return False


def _window_saved_memory(window: list[dict]) -> bool:
    """True when the main agent already mutated memory inside the window.

    The tool-result rows for memory_save / memory_delete are JSON with a
    top-level "saved" / "deleted" key (memory.read results have neither),
    so a string scan over tool rows is exact without re-parsing every
    payload shape."""
    for m in window:
        if m.get("role") != "tool":
            continue
        content = str(m.get("content") or "")
        if '"saved":' in content or '"deleted":' in content:
            return True
    return False


async def plan_extraction(conversation_id: int) -> dict | None:
    """Evaluate the gates over the unreviewed window.

    Returns the snapshot to run ({"window", "boundary"}) or a skip decision
    ({"skip", "boundary"}) - skips advance the cursor before returning, so
    the skipped content is never re-scanned. None: nothing new to review.
    A failure here (DB errors) propagates: the cursor must not move on a
    failed gate evaluation either."""
    from backend.db.database import get_messages

    cursor = await _get_cursor(conversation_id)
    rows = await get_messages(conversation_id)
    window = [r for r in rows if int(r["id"]) > cursor]
    if not window:
        return None
    boundary = int(window[-1]["id"])
    if _window_saved_memory(window):
        # Gate 1 (zcode's direct-memory-write): the main agent already
        # wrote this window's facts; a second pass would duplicate them.
        await _set_cursor(conversation_id, boundary)
        return {"skip": "direct-memory-write", "boundary": boundary}
    if not _genuine_user_prose(window):
        # Gate 2 (no-user-prose): tool-only or synthetic turns don't
        # trigger a model call.
        await _set_cursor(conversation_id, boundary)
        return {"skip": "no-user-prose", "boundary": boundary}
    return {"window": window, "boundary": boundary}


# ------------------------------------------------------------------ writer

def _manifest(workspace: str | None) -> str:
    """The existing-memory index handed to the writer so it updates rather
    than duplicates. ponytail: index order (append = oldest first), tail
    kept under the cap - mtime-sorting like zcode buys nothing at this
    size; revisit if stores grow past a few hundred entries."""
    idx = memory.memory_dir(workspace) / "MEMORY.md"
    try:
        text = idx.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [
        ln.rstrip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if len(lines) > MAX_MANIFEST_ENTRIES:
        lines = lines[-MAX_MANIFEST_ENTRIES:]
    return "\n".join(lines)


def _writer_prompt(window: list[dict], manifest: str) -> str:
    lines = []
    for m in window:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = " ".join(str(m.get("content") or "").split())
        if content:
            lines.append(f"{m['role']}: {content}")
    body = "\n\n".join(lines)
    if len(body) > MAX_WINDOW_CHARS:
        body = "…[older content omitted]\n" + body[-MAX_WINDOW_CHARS:]
    return (
        "Review the conversation window below and maintain persistent "
        "memory for this workspace. Work ONLY from the window's content: "
        "do not investigate the repository, and never save anything the "
        "repo or the tracker already records.\n\n"
        "Work in exactly two turns: turn one, read every existing "
        "memory you might touch (parallel memory_read calls, no "
        "writing); turn two, write everything (parallel memory_save / "
        "memory_delete) and finish. "
        "Update an existing memory by re-saving it under the SAME name "
        "(the slug in the (slug.md) link below) rather than creating a "
        "near-duplicate; create a new file only when no existing memory "
        "covers the fact. "
        "Never run shell commands or verification greps - the window is "
        "your only evidence.\n\n"
        "Existing memories:\n"
        f"{manifest or '(none yet)'}\n\n"
        "Conversation window:\n\n"
        f"{body}\n\n"
        f"If nothing in the window is worth saving, output only "
        f"'{NOTHING_TO_SAVE}'."
        # Charter replay by identity (#345): the store's own constant,
        # never a restated copy that can drift.
        f"\n\n---\n\n{memory._WHEN_TO_SAVE}"
    )


async def run_writer(
    window: list[dict], workspace: str | None
) -> dict:
    """One confined writer run over the window. Never raises
    (run_sub_agent contract): status is 'completed', 'error', or
    'cancelled'."""
    return await subagents.run_sub_agent(
        WRITER_DEF,
        _writer_prompt(window, _manifest(workspace)),
        workspace=str(workspace or "."),
        # Auto-allowed writes (#345): no access-mode gate. Containment is
        # the memory tools themselves - they resolve slugs inside the
        # memory root and are the only catalogue the writer has.
        gate=None,
        memory_workspace=memory_workspace_for(workspace),
    )


# ------------------------------------------------------- scheduler (one slot)

# conversation_id -> pending snapshot; single slot: a newer schedule
# REPLACES the pending one (coalescing by replacement, not a queue).
_pending: dict[int, dict] = {}
_inflight: set[int] = set()
# Strong references to running drains (scheduler precedent: an
# unreferenced create_task can be garbage-collected mid-run).
_tasks: set[asyncio.Task] = set()


def _reset_state() -> None:
    """Test seam: clear scheduler bookkeeping between tests."""
    _pending.clear()
    _inflight.clear()
    _tasks.clear()


async def _run(plan: dict) -> None:
    """One extraction pass; the cursor advances only on a completed run,
    so failed/cancelled content rides into the next window."""
    result = await run_writer(plan["window"], plan["workspace"])
    if result.get("status") == "completed":
        await _set_cursor(plan["conversation_id"], plan["boundary"])


async def _drain(conversation_id: int) -> None:
    while True:
        plan = _pending.pop(conversation_id, None)
        if plan is None:
            _inflight.discard(conversation_id)
            return
        try:
            await _run(plan)
        except Exception:
            log.exception("memory extraction drain failed")


def _launch(conversation_id: int) -> None:
    _inflight.add(conversation_id)
    task = asyncio.create_task(_drain(conversation_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def schedule(conversation_id: int, workspace: str | None) -> None:
    """Turn-completion hook (#341/#345): gates now (cheap, no model call),
    model pass in a detached task. Never raises; callers may also wrap
    defensively - the turn must not fail or wait on extraction."""
    try:
        from backend.agent.tools import memory_enabled

        if not memory_enabled():
            return
        plan = await plan_extraction(conversation_id)
        if plan is None or "skip" in plan:
            return
        plan["conversation_id"] = conversation_id
        plan["workspace"] = workspace
        if conversation_id in _inflight:
            _pending[conversation_id] = plan
            return
        _pending[conversation_id] = plan
        _launch(conversation_id)
    except Exception as e:  # noqa: BLE001 - never fail the turn
        log.warning("memory extraction scheduling failed: %s", e)
