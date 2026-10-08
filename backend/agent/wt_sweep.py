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
import asyncio
import logging
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

log = logging.getLogger(__name__)

_last_sweep = 0.0
_MIN_INTERVAL = 3600.0  # once per process start, then at most hourly

# #357: chat worktrees whose post-run retirement was missed (teardown
# cancelled or timed out, git failure, unreachable remote host) are
# enqueued here and retried on every sweep tick - no age gate and no
# dead-chat gate (the post-run hook retires a live chat's tree by
# design; only the clean/no-residue gates decide). Keyed
# workspace -> chat ids.
_enqueued: dict[str, set[str]] = {}

# Refusals that are POLICY, not misses: the residue protocol owns
# surfacing these and the sweep would refuse them again anyway.
RETIRE_POLICY_REFUSALS = frozenset(
    {"no chat worktree", "dirty", "run residue present"}
)

_ticker_task: asyncio.Task | None = None


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


def enqueue_missed_retirement(workspace: str, chat_id: int | str) -> None:
    """Record a chat worktree whose post-run retirement did not happen.

    Called from teardown when retirement was skipped or refused for a
    NON-policy reason (cancellation, timeout, git failure, unreachable
    host). The tree retires on the next sweep tick without the age or
    dead-chat gates - the post-run hook retires live chats' trees by
    design, so only the clean/no-residue gates still decide (#357).
    """
    ws = (workspace or "").strip()
    if not ws or ws.startswith("remote:"):
        return
    _enqueued.setdefault(ws, set()).add(str(chat_id))


def _take_enqueued_local() -> list[tuple[str, str]]:
    """Pop the LOCAL-namespace enqueue entries (snapshot-and-clear)."""
    items = [
        (ws, cid) for ws, ids in _enqueued.items() if not ws.startswith("remote:")
        for cid in ids
    ]
    for ws, cid in items:
        ids = _enqueued.get(ws)
        if ids is not None:
            ids.discard(cid)
            if not ids:
                _enqueued.pop(ws, None)
    return items


def _snapshot_enqueued_remote() -> list[tuple[str, str]]:
    """Read the REMOTE-namespace enqueue entries without clearing them.

    A remote tree that stays dirty (or the host stays unreachable) keeps
    its entry and is retried on the next tick; 'no chat worktree'
    retires it from the queue below.
    """
    return [
        (ws, cid) for ws, ids in _enqueued.items() if ws.startswith("remote:")
        for cid in ids
    ]


def _drop_enqueued(workspace: str, chat_id: str) -> None:
    ids = _enqueued.get(workspace)
    if ids is not None:
        ids.discard(chat_id)
        if not ids:
            _enqueued.pop(workspace, None)


async def sweep_enqueued_retirements() -> dict:
    """Retry the missed retirements (#357): local trees through the
    #329 hook (no age gate - enqueued trees were in use moments ago),
    remote trees through the gateway twin (#334). Policy refusals leave
    the queue untouched (the residue protocol owns those); 'no chat
    worktree' drops the entry - there is nothing left to retire.
    Never raises over an individual tree."""
    from backend.agent.worktrees import retire_chat_worktree

    swept: list[str] = []
    dropped: list[str] = []
    kept: list[str] = []

    for ws, cid in _take_enqueued_local():
        try:
            res = await retire_chat_worktree(ws, cid)
        except Exception:  # noqa: BLE001 - re-enqueue, try again next tick
            log.exception("enqueued retirement failed: %s chat-%s", ws, cid)
            enqueue_missed_retirement(ws, cid)
            kept.append(f"{ws} chat-{cid}")
            continue
        if res.get("retired"):
            swept.append(str(res.get("path") or f"{ws} chat-{cid}"))
        elif res.get("reason") in RETIRE_POLICY_REFUSALS:
            _drop_enqueued(ws, cid)
            dropped.append(f"{ws} chat-{cid}")
        else:
            # transient miss (git failure, gone mid-flight): retry next tick
            enqueue_missed_retirement(ws, cid)
            kept.append(f"{ws} chat-{cid}")

    from backend.agent import wt_remote

    for ws, cid in _snapshot_enqueued_remote():
        try:
            res = await wt_remote.retire_chat_worktree(ws, cid)
        except Exception:  # noqa: BLE001 - entry stays, retried next tick
            log.exception("enqueued remote retirement failed: %s chat-%s", ws, cid)
            kept.append(f"{ws} chat-{cid}")
            continue
        if res.get("retired"):
            _drop_enqueued(ws, cid)
            swept.append(f"{ws} chat-{cid}")
        elif res.get("reason") in RETIRE_POLICY_REFUSALS:
            _drop_enqueued(ws, cid)
            dropped.append(f"{ws} chat-{cid}")
        else:
            # unreachable host / transient git failure: entry stays
            kept.append(f"{ws} chat-{cid}")

    return {"swept": swept, "dropped": dropped, "kept": kept}


async def _sweep_ticker_tick() -> None:
    """One sweep opportunity, shared by the boot hook and the ticker."""
    if not should_sweep():
        return
    try:
        result = await sweep_stale_chat_worktrees()
        if result.get("swept") or result.get("error"):
            log.info("chat-worktree sweep: %s", result)
        enq = await sweep_enqueued_retirements()
        if enq.get("swept") or enq.get("dropped"):
            log.info("enqueued retirement sweep (#357): %s", enq)
    except Exception:  # noqa: BLE001 - maintenance never blocks serving
        log.exception("chat-worktree sweep failed")


async def _sweep_ticker() -> None:
    """Hourly sweep opportunist (#357): 'should_sweep' promises at most
    hourly after the boot sweep; the boot hook alone left long-lived
    backend processes with exactly one sweep per boot. Sleeps first:
    boot just swept; a short-lived process never ticks at all."""
    while True:
        await asyncio.sleep(_MIN_INTERVAL)
        await _sweep_ticker_tick()


def start_sweep_ticker() -> None:
    """Start the hourly sweep ticker (idempotent; a running ticker is
    left alone - a second one would only double no-op rate-limited
    ticks). Best-effort: without a running loop this is a no-op and
    boot's single sweep still happened."""
    global _ticker_task
    if _ticker_task is not None and not _ticker_task.done():
        return
    try:
        _ticker_task = asyncio.get_running_loop().create_task(_sweep_ticker())
    except RuntimeError:
        _ticker_task = None


async def stop_sweep_ticker() -> None:
    """Stop the ticker. The pending sleep is awaited to completion (not
    cancelled) so no asyncio 'task destroyed while pending' noise is
    emitted at shutdown."""
    global _ticker_task
    task, _ticker_task = _ticker_task, None
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except BaseException:  # noqa: BLE001 - shutdown never blocks here
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
