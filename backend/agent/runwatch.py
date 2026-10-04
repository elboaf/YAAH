"""Run-in-flight state for the status-strip badge (issue #290).

Derived state, never announcements: the badge polls `git worktree list`
(same TTL shape as gitinfo) and reports which run worktrees exist under
the deterministic per-chat namespace `.scratch/chat-<id>/` — the path
contract AGENTS.md prescribes (ADR-0010). Chip attribution is path
arithmetic, not heuristics over worktree names.

Kept out of the tool layer on purpose: this is app state, not a model
tool. A `.scratch/run-YYYYMMDD-<slug>` worktree from before the #290
namespace stays invisible here — it predates the contract, and the chip
must not light for residue the runs of that era managed their own way.
"""
import time
from pathlib import Path

from backend.agent.gitinfo import _run_git

# (workspace root, landing target) -> (monotonic time captured, runs).
# One `git worktree list` burst per TTL per workspace+target no matter how
# many pollers ask — the UI polls every ~2s while a session is open.
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_TTL = 2.0  # seconds; matches the UI poll cadence


def invalidate_run_caches(root: Path | str | None = None) -> None:
    """Drop cached run lists (tests change the repo behind the TTL's back;
    a None root drops every workspace's entry)."""
    if root is None:
        _cache.clear()
        return
    key_root = str(Path(root))
    for key in [k for k in _cache if k[0] == key_root]:
        _cache.pop(key, None)


def classify_run_residue(dirty: bool, merged: bool) -> str:
    """The residue protocol as a pure mapping (issue #290, part 3).

    clean    — no uncommitted changes and the run branch is fully merged
               into the landing target: landed-and-forgotten; a run start
               may auto-remove it.
    dirty    — uncommitted changes exist: untouched and surfaced. Never
               silently deleted.
    unmerged — committed work has not landed: untouched and surfaced; the
               user says "land it" or "scrap it".
    """
    if dirty:
        return "dirty"
    return "clean" if merged else "unmerged"


def _parse_chat_worktrees(root: Path, porcelain: str) -> list[dict]:
    """Worktree-list entries under `.scratch/chat-<id>/`, attributed by
    path prefix arithmetic. Detached checkouts (no branch line) are
    skipped — a run branch is the landable unit the badge names."""
    collected: list[dict] = []
    entry: dict = {}
    for line in porcelain.splitlines():
        if not line.strip():
            if entry.get("path") and entry.get("branch"):
                collected.append(entry)
            entry = {}
            continue
        head, _, rest = line.partition(" ")
        if head == "worktree":
            entry = {"path": rest}
        elif head == "branch" and entry:
            entry["branch"] = rest.removeprefix("refs/heads/")
    if entry.get("path") and entry.get("branch"):
        collected.append(entry)

    runs: list[dict] = []
    for entry in collected:
        try:
            rel = Path(entry["path"]).relative_to(root)
        except ValueError:
            continue  # the primary worktree itself — never listed
        parts = rel.parts
        if len(parts) != 3 or parts[0] != ".scratch" or not parts[1].startswith("chat-"):
            continue
        chat_id = parts[1][len("chat-"):]
        if not chat_id:
            continue
        runs.append({
            "branch": entry["branch"],
            "chat_id": chat_id,
            "leaf": parts[2],
            "path": str(root / rel),
        })
    return runs


async def run_worktrees(root: Path | str, target: str | None = None) -> list[dict]:
    """The `.scratch/chat-<id>/` run worktrees of the workspace, each with
    its residue state against `target` (the landing target — the chat's
    selected branch, falling back to the checked-out branch). Empty when
    the workspace is not a git repo or no run worktrees exist."""
    root = Path(root)
    target = target or ""
    key = (str(root), target)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _TTL:
        return cached[1]

    rc, out = await _run_git(
        root, "-c", "core.quotePath=false", "worktree", "list", "--porcelain"
    )
    runs = _parse_chat_worktrees(root, out) if rc == 0 else []

    for run in runs:
        # Dirty check inside the run worktree itself.
        drc, dout = await _run_git(Path(run["path"]), "status", "--porcelain")
        dirty = drc != 0 or bool(dout.strip())
        # Merged check against the landing target (is-ancestor exits 0
        # when the run branch is fully merged, 1 when not, other on error
        # — an unknown target must never read as "landed").
        mrc = 1
        if target:
            mrc, _ = await _run_git(
                root, "merge-base", "--is-ancestor", run["branch"], target
            )
        run["dirty"] = dirty
        run["merged"] = target != "" and mrc == 0
        run["residue"] = classify_run_residue(dirty, run["merged"])

    _cache[key] = (now, runs)
    return runs
