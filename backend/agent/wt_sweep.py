"""Per-chat worktree maintenance (issue #277; lifecycle per ADR-0010 as
amended 2026-10-06 by #329, land-means-clean).

Primary retirement is post-run, in the harness: a clean, residue-free
chat worktree is removed when its run ends, freeing any branch checkout.
The sweeper is the backstop for residue the run-end path missed:
clean-only, dead-chat-only, past an age threshold.
clean-only, dead-chat-only, past an age threshold. "Clean" is a whole
`git status --porcelain` on the chat worktree — which a nested run
worktree (`.scratch/chat-<id>/run`) shows as an untracked entry, so an
in-flight or residue run automatically pins the tree as dirty and it is
never swept. Deletion never touches a conversation row: chat deletion
alone does not remove the tree (ADR-0010), the sweeper is what retires
it once the chat is gone AND the tree sat clean past the threshold.

KNOWN DEFECT (#329, unfixed here): the dead-chat gate below is
per-WORKSPACE - any live conversation pins every tree in that
workspace, so in the main workspace the sweeper never fires. The fix
is a per-chat gate (conversation row gone + clean + age).
"""
import time
from pathlib import Path

from backend.agent.gitinfo import _run_git

# A chat worktree with no uncommitted changes and no residue past this
# age is auto-pruned. Idle trees cost disk; this bounds it.
MAX_IDLE_SECONDS = 30 * 24 * 3600  # 30 days

_last_sweep = 0.0
_MIN_INTERVAL = 3600.0  # once per process start, then at most hourly


async def sweep_stale_chat_worktrees(max_idle_seconds: int = MAX_IDLE_SECONDS) -> dict:
    """Retire clean chat worktrees of dead chats past the age threshold.

    Returns {checked, swept: [paths], kept} for logging; never raises
    over an individual tree — a busy or unreadable tree is just kept
    this round.
    """
    from backend.db.database import get_db

    swept: list[str] = []
    checked = 0
    try:
        db = await get_db()
        try:
            cur = await db.execute("SELECT path FROM workspaces")
            ws_rows = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute(
                "SELECT DISTINCT workspace FROM conversations WHERE workspace IS NOT NULL"
            )
            live = {str(r["workspace"]) for r in await cur.fetchall()}
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 - the sweeper must never break boot
        return {"checked": 0, "swept": [], "kept": 0, "error": "db unavailable"}

    now = time.time()
    for row in ws_rows:
        ws = str(row.get("path") or "")
        if not ws or ws.startswith("remote:") or ws in live:
            continue
        # Find this workspace's chat worktrees by path arithmetic instead
        # of trusting any single chat id.
        scratch = Path(ws) / ".scratch"
        if not scratch.is_dir():
            continue
        for child in scratch.iterdir():
            if not child.is_dir() or not child.name.startswith("chat-"):
                continue
            checked += 1
            # Age gate on mtime: cheap; the clean gate is authoritative.
            try:
                age = now - child.stat().st_mtime
            except OSError:
                continue
            if age < max_idle_seconds:
                continue
            rc, out = await _run_git(child, "status", "--porcelain")
            if rc != 0 or (out or "").strip():
                continue  # dirty, in-flight, or residue: never swept
            rc2, _ = await _run_git(ws, "worktree", "remove", str(child))
            if rc2 == 0:
                swept.append(str(child))
    return {"checked": checked, "swept": swept, "kept": checked - len(swept)}


def should_sweep(now: float | None = None) -> bool:
    """Rate limit: once per process start, then at most hourly. True
    marks the window consumed."""
    global _last_sweep
    now = time.time() if now is None else now
    if _last_sweep == 0.0 or now - _last_sweep >= _MIN_INTERVAL:
        _last_sweep = now
        return True
    return False
