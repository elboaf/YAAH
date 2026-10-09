"""One-time legacy residue cleanup pass (#364; ADR-0017 closes isolation).

The per-chat worktree design is gone (#360-#362): runs execute directly
in the workspace and nothing creates chat worktrees, run worktrees, or
run/* / wip/* branches anymore. Upgraded installs still carry the dead
design's residue - registered chat worktrees holding branch checkouts
hostage, and merged run/wip refs squatting in `git branch`. This pass
heals exactly that, once per database, at boot (before the scheduler,
like the #132 repair):

- clean chat trees (<workspace>/.scratch/chat-<id>/, registered or
  legacy standalone clones) are removed, releasing their branch
  checkouts;
- dirty trees are never touched - only surfaced with path and reason
  (a tree whose NESTED run worktree is dirty counts as dirty: removal
  is recursive and would delete that work with the parent);
- local run/* and wip/* branches whose commits are contained in some
  other local branch (or the parent's own checkout) are deleted with
  `-D`, but only after that containment is proven (`branch --contains`)
  - the modern shape of #343's safe-mode reaping; unmerged ones survive
  and are surfaced.

One summary report comes back for logging. The one-time marker is
written to the database BEFORE any tree is touched, so the pass can
provably never run twice - a crash mid-sweep leaves residue surfaced
for a manual pass rather than risking a repeat of a destructive sweep.
"""

import logging
import os
import shutil
import stat
from pathlib import Path

from backend.agent.gitexec import _run_git_local

log = logging.getLogger(__name__)

_MARKER_KEY = "legacy_residue_cleanup"


async def _marker_set(db) -> bool:
    cur = await db.execute("SELECT value FROM app_meta WHERE key = ?", (_MARKER_KEY,))
    return await cur.fetchone() is not None


async def run_legacy_residue_cleanup() -> dict:
    """The #364 pass. Runs at most once per database (marker row); every
    later boot is a no-op. Never raises - boot must not break over
    residue, and per-tree failures surface instead of aborting."""
    from backend.db.database import get_db

    try:
        db = await get_db()
    except Exception:  # noqa: BLE001 - nothing ran; next boot retries
        return {"error": "db unavailable"}

    try:
        if await _marker_set(db):
            return {"skipped": True}
        # Marker first (#364: "provably never run twice"). ponytail:
        # a crash between this commit and the sweep leaves the residue
        # standing forever-by-design; surfacing beats re-running a
        # destructive pass.
        await db.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?)",
            (_MARKER_KEY, "done"),
        )
        await db.commit()

        cur = await db.execute("SELECT path FROM workspaces")
        ws_rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    removed_trees: list[str] = []
    surfaced: list[dict] = []
    reaped_branches: list[str] = []
    kept_branches: list[str] = []

    for row in ws_rows:
        ws = str(row.get("path") or "")
        if not ws or ws.startswith("remote:") or not Path(ws).is_dir():
            continue
        try:
            scratch = Path(ws) / ".scratch"
            if scratch.is_dir():
                for child in scratch.iterdir():
                    if not child.is_dir() or not child.name.startswith("chat-"):
                        continue
                    try:
                        int(child.name.removeprefix("chat-"))
                    except ValueError:
                        continue  # not a chat worktree (e.g. chat-mic-gate)
                    reason = await _dirty_reason(child)
                    if reason is not None:
                        surfaced.append({"path": str(child), "reason": reason})
                        continue
                    await _remove_tree(ws, child, removed_trees, surfaced)

            # Branch reaping runs for EVERY local workspace - a workspace
            # with no .scratch can still carry dead run/wip refs.
            reaped, kept = await _reap_merged_branches(ws)
            reaped_branches.extend(reaped)
            kept_branches.extend(kept)
        except Exception as e:  # noqa: BLE001 - surface, never break boot
            surfaced.append({"path": ws, "reason": f"unexpected: {e}"})

    return {
        "ran_once": True,
        "removed_trees": removed_trees,
        "surfaced": surfaced,
        "reaped_branches": reaped_branches,
        "kept_branches": kept_branches,
    }


async def _dirty_reason(tree: Path) -> str | None:
    """Why this chat tree must not be touched, or None when clean.

    The `status` gate is the authority - anything it flags (including
    unknown layouts) keeps the tree. The nested-run walk only upgrades
    the reason text for the known shape (<chat>/.scratch/chat-<id>/run,
    the deterministic run-worktree nest).
    """
    rc, out = await _run_git_local(str(tree), "status", "--porcelain")
    if rc != 0:
        return "git status failed - not touchable"
    if (out or "").strip():
        return "dirty tree"
    scratch = tree / ".scratch"
    if scratch.is_dir():
        for run in scratch.glob("chat-*/run"):
            if not run.is_dir():
                continue
            rc, out = await _run_git_local(str(run), "status", "--porcelain")
            if rc != 0 or (out or "").strip():
                return "dirty tree (nested run worktree has changes)"
    return None


async def _remove_tree(ws: str, child: Path, removed: list, surfaced: list) -> None:
    """Remove one clean chat tree: registered worktrees through git
    (releases the branch checkout), legacy standalone clones (.git a
    directory - git refuses those) through a read-only-bit-aware rmtree.
    A registered tree git refuses for an unlisted reason is surfaced,
    never rmtree'd."""
    rc, _ = await _run_git_local(ws, "worktree", "remove", str(child))
    if rc == 0:
        removed.append(str(child))
        _prune_empty_parents(ws, child)
        return
    if (child / ".git").is_file():
        surfaced.append(
            {"path": str(child), "reason": "clean but git refused removal"}
        )
        return
    try:
        _rmtree_readonly(child)
        removed.append(str(child))
        _prune_empty_parents(ws, child)
    except OSError:
        surfaced.append({"path": str(child), "reason": "removal failed"})


def _prune_empty_parents(ws: str, child: Path) -> None:
    """Best-effort rmdir of the empty husks nested run worktrees leave
    behind (git creates the intermediate dirs, nothing removes them).
    Cosmetic only; refuses non-empty dirs silently."""
    ws_path = Path(ws).resolve()
    for parent in child.parents:
        if parent.exists() and parent.resolve() == ws_path:
            break
        try:
            parent.rmdir()
        except OSError:
            break


def _rmtree_readonly(path: Path) -> None:
    """rmtree that clears Windows read-only bits (git marks pack files
    0444) - the removal the old sweeper used for standalone clones."""

    def _reset(fn, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            fn(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_reset)


async def _reap_merged_branches(ws: str) -> tuple[list[str], list[str]]:
    """Delete local run/* and wip/* refs proven contained in some OTHER
    local branch (the parent tree's own checkout counts: its commits are
    as reachable as a branch's). Unmerged refs survive - `kept`."""
    reaped: list[str] = []
    kept: list[str] = []
    rc, out = await _run_git_local(
        ws, "branch", "--list", "run/*", "wip/*", "--format=%(refname:short)"
    )
    if rc != 0:
        return [], []
    for name in [ln for ln in (out or "").splitlines() if ln.strip()]:
        rc, contains = await _run_git_local(ws, "branch", "--contains", name)
        # `--contains` marks branches, it does not indent to fixed
        # columns: `* ` = current branch, `+ ` = checked out in a linked
        # worktree, two spaces otherwise - and the executor strips the
        # WHOLE output, so an unmarked first line loses its indent
        # entirely. Match the marker only where it can be: prefix
        # `* `/`+ ` on the stripped line (ref names never contain
        # spaces, so the marker's trailing space is unambiguous).
        others = []
        for ln in (contains or "").splitlines():
            bare = ln.strip()
            if bare[:1] in ("*", "+") and bare[1:2] == " ":
                bare = bare[2:]
            if bare and bare != name:
                others.append(bare)
        if rc != 0 or not others:
            kept.append(name)  # not contained anywhere else: unmerged, survive
            continue
        rc, _ = await _run_git_local(ws, "branch", "-D", name)
        if rc == 0:
            reaped.append(name)
        else:
            kept.append(name)
    return reaped, kept
