"""Per-chat worktree maintenance (#277 slice D; lifecycle per #329/ADR-0010).

The sweeper is one of two retirement mechanisms for chat worktrees - the
other is the post-run hook (``worktrees.retire_chat_worktree``, called at
run end from loop.py). The sweeper catches what the hook leaves behind:
trees of chats that no longer exist at all. Retirement is clean-only and
residue-only-never: a tree that is dirty, or that still holds the
deterministic run namespace (``<chat>/run``, ``<chat>/.scratch`` -
git-invisible since #321, so pinned by EXISTENCE per #330), is never
swept.

Gating is PER CHAT (#329): a chat is dead when its conversation row is
gone - detected by listing the ``.scratch/chat-*`` dirs and resolving
each id against the conversations table - not by skipping whole
workspaces that have any live conversation. The main workspace always
has live chats; the old per-workspace gate made the sweeper a permanent
no-op exactly where the pile grows.

Pre-#277 standalone clones (``.git`` a directory, not worktree-registered)
are retirably visible too: the same per-chat checks run, and removal
falls back to plain rmtree when ``git worktree remove`` refuses them.
"""
import os
import shutil
import time
from pathlib import Path

from backend.agent.gitinfo import _run_git
from backend.agent.worktrees import (
    chat_worktree_has_run_tree,
    reap_merged_run_branches,
    run_branch_candidates,
)

# A chat worktree with no uncommitted changes and no residue past this
# age is auto-pruned. Idle trees cost disk; this bounds it.
MAX_IDLE_SECONDS = 30 * 24 * 3600  # 30 days

_last_sweep = 0.0
_MIN_INTERVAL = 3600.0  # once per process start, then at most hourly


def _rmtree_readonly(path: Path) -> None:
    """rmtree that clears Windows read-only bits (git marks pack files
    0444). onerror-style retries are deprecated on 3.12+; do it with
    explicit resets."""
    import stat

    def _reset(fn, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            fn(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_reset)


async def sweep_stale_chat_worktrees(max_idle_seconds: int = MAX_IDLE_SECONDS) -> dict:
    """Retire clean chat worktrees of dead chats past the age threshold.

    Returns {checked, swept: [paths], kept, reaped_branches} for
    logging; never raises over an individual tree - a busy or unreadable
    tree is just kept this round.
    """
    from backend.db.database import get_db

    swept: list[str] = []
    reaped_branches: list[str] = []
    checked = 0
    try:
        db = await get_db()
        try:
            cur = await db.execute(
                "SELECT path FROM workspaces"
            )
            ws_rows = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute(
                "SELECT DISTINCT id FROM conversations"
            )
            live_ids = {str(r["id"]) for r in await cur.fetchall()}
        finally:
            await db.close()
    except Exception:  # noqa: BLE001 - the sweeper must never break boot
        return {"checked": 0, "swept": [], "kept": 0, "error": "db unavailable"}

    now = time.time()
    for row in ws_rows:
        ws = str(row.get("path") or "")
        if not ws or ws.startswith("remote:"):
            continue
        # Find this workspace's chat worktrees by path arithmetic instead
        # of trusting any single chat id. The chat-id parse IS the dead
        # chat gate (#329): a dir whose id has no conversation row is
        # sweepable no matter how many OTHER chats of this workspace are
        # live right now.
        scratch = Path(ws) / ".scratch"
        if not scratch.is_dir():
            continue
        for child in scratch.iterdir():
            if not child.is_dir() or not child.name.startswith("chat-"):
                continue
            chat_id = child.name.removeprefix("chat-")
            try:
                int(chat_id)
            except ValueError:
                continue  # not a per-chat worktree (e.g. chat-mic-gate)
            if chat_id in live_ids:
                continue  # a live chat's tree is never swept here
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
                continue  # dirty: never swept
            # #330: the run namespace is git-invisible (#321), so pin by
            # existence - a live run or residue keeps the tree standing.
            if await chat_worktree_has_run_tree(child):
                continue
            # #343: capture run-branch names BEFORE the remove empties
            # the registry; reap AFTER it frees the checkouts. Same
            # before/after ordering the post-run hook uses.
            candidates = await run_branch_candidates(child, chat_id)
            brc, br = await _run_git(child, "branch", "--show-current")
            merged_into = (br or "").strip() or None if brc == 0 else None
            rc2, _ = await _run_git(ws, "worktree", "remove", str(child))
            if rc2 != 0:
                # Legacy pre-#277 standalone clone (#329): `.git` is a
                # directory, git refuses - but it is clean, dead, aged,
                # and unregistered, so plain removal retires it all the
                # same. Only when it is genuinely NOT worktree-registered
                # (no `.git` FILE); a registered tree failing `worktree
                # remove` is kept for surface, never rmtree'd.
                if not (child / ".git").is_file():
                    try:
                        _rmtree_readonly(child)
                        swept.append(str(child))
                    except OSError:
                        pass
                continue
            swept.append(str(child))
            reaped_branches.extend(
                await reap_merged_run_branches(ws, candidates, merged_into)
            )
    return {
        "checked": checked,
        "swept": swept,
        "kept": checked - len(swept),
        "reaped_branches": reaped_branches,
    }


def should_sweep(now: float | None = None) -> bool:
    """Rate limit: once per process start, then at most hourly. True
    marks the window consumed."""
    global _last_sweep
    now = time.time() if now is None else now
    if _last_sweep == 0.0 or now - _last_sweep >= _MIN_INTERVAL:
        _last_sweep = now
        return True
    return False
