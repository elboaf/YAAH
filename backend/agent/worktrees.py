"""Issue #58: a git worktree per writing agent, mutex-serialized merge-back.

The harness (not prompt convention) gives every writing agent its own
worktree and rebinds the agent's `workspace` string to it for the
duration of the run. Every tool already resolves paths and cwd through
`workspace_root()`, so rebinding isolates the file tools, the shell
tools, and the `git_*` tools in one move — an isolated agent's
`git_commit` lands in its own worktree, never in the main tree.

Honesty notes (from the issue text, kept honest here):
- bash confinement is best-effort (prompt + cwd + gate). Arbitrary child
  processes can escape by absolute path. That is accepted: each agent
  escapes into its OWN worktree, so escapes are irrelevant.
- The cross-process merge lock only protects writers who go through the
  harness, same enforcement principle as the rebinding itself.

Layout: worktrees live at `<main-root>/.yaah/worktrees/<chat-id>` on
branches `agent/<chat-id>/<run-id>` — one worktree per chat session
(adr/0003), named by chat id so a restart can recover the binding from
the path shape. The path is excluded via
`.git/info/exclude` (never the user's tracked .gitignore), branches are
filtered out of the UI branch list, and search prunes the `.yaah`
directory (see tools.IGNORED_DIRS).

Merge rules (issue decisions 1-6, amended by docs/adr/0003, REVISED
branch-first 2026-09-23):
- Sub-agents never merge: they report the branch on the first line of
  their final message (results are clipped; the parent must always be
  able to act on the branch name).
- Top-level chat agents are bound to ONE worktree for the chat's whole
  session (adr/0003): the first isolating workspace mutation creates it,
  and it is never released mid-session. The harness NEVER merges into the
  main tree on its own: commits stay on the session branch until the agent
  explicitly integrates requested code changes with git_merge_back. The old
  per-turn auto-merge hid the work in a phantom branch while the UI claimed
  master — and made a follow-up "push" publish the wrong ref.
  Uncommitted worktree state is left in place across turns — turn N+1
  works in exactly the tree turn N left behind. A session whose branch
  carries no commits and no uncommitted files is drained at turn end
  (nothing to preserve); the trash/salvage teardown of adr/0002 runs
  once, at session end (release_session): chat deletion, drain, or the
  reaper for orphans.
- Merge-back refuses zero new commits, uncommitted main-tree files that
  the merge would overwrite (dirty *overlap* — never stash), mid-merge
  state, and merge conflicts (aborting) — surfaced, never papered over.
  Unrelated WIP does not block a merge: git's own overlap-aware
  pre-flight decides (see docs/adr/0001). A refused merge keeps the
  session bound: the next turn can retry it (or the model can call
  git_merge_back itself) instead of the commits stranding on a branch
  the next turn cannot see.
- Trash-detecting teardown (issue #98, docs/adr/0002) lives in
  release_session and finalize_sub_agent: uncommitted files that are
  provably harness-generated (write provenance: command-output
  captures; or machine-shape fingerprints: JSON, diffs, base64 walls,
  .log/.tmp-style names) are dropped, not salvaged — a stray log must
  not outlive the session as litter. Authored-looking files are
  salvaged to a patch, never silently deleted.
- Recovery over tidiness: release_session runs the adr/0002 trash
  contract once (drops and salvage patches) but leaves the worktree
  directory and the branch in place — the reaper performs the physical
  teardown after the worktree TTL (48h default). The branch is deleted
  at release only when git confirms it is fully merged into the main
  HEAD; unmerged branches always survive the reaper (the branch is the
  record of the work — the user deletes it, never the reaper).

Main-tree sync (issue #98): the user's folder — the only tree they can
see — is fast-forwarded to upstream on a background cadence (ff-only,
mutex-serialized, overlap-aware), so a refused or crashed merge-back
can never leave the visible folder silently behind the released work.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

# Placement classification lives in wtclassify (ADR 0007 ticket 2: pure
# decision logic, no lifecycle state). Re-exported so existing callers
# (`worktrees.should_isolate`) keep working.
from backend.agent.wtclassify import (  # noqa: F401
    _NON_WORKSPACE_MUTATIONS,
    _PLACEMENT_READ_TOOLS,
    _DIRECT_MAIN_TREE_TOOLS,
    _CHILD_DIRECT_WRITERS,
    _readonly_segment,
    _readonly_shell_command,
    should_isolate,
)
# Git process plumbing lives in gitproc (ADR 0007 ticket 2). `_git_exe`
# keeps its old (private) name as a re-export: file_changes and
# git_activity still import it under that name; they migrate to
# `gitproc.git_exe` in ticket 4.
from backend.agent.gitproc import NO_WINDOW as _NO_WINDOW  # noqa: F401
from backend.agent.gitproc import NEW_SESSION as _NEW_SESSION  # noqa: F401
from backend.agent.gitproc import git_exe as _git_exe  # noqa: F401



BRANCH_PREFIX = "agent/"
WT_DIRNAME = "worktrees"
WT_PARENT = ".yaah"

# Merge mutex: in-process per-root asyncio.Lock + cross-process lockfile
# at <root>/.git/yaah-merge.lock (inside .git: never in status, never
# committed). Merges are seconds-long; a lockfile older than STALE_LOCK is
# a crashed holder, not a slow merge — take it over.
MUTEX_TIMEOUT_SECONDS = 120.0
STALE_LOCK_SECONDS = 300.0

# Reaper TTLs. Worktree dirs: crashed runs must not pin the workspace
# forever, but recovery outranks tidiness — an agent's worktree is the
# only place uncommitted work lives, so teardown is deliberately slow
# (default 48h, YAAH_WORKTREE_TTL_SECONDS overrides). Branches: kept for
# inspection a few days (issue decision 4), then pruned.
DEFAULT_WORKTREE_TTL_SECONDS = 48 * 3600.0
BRANCH_TTL_SECONDS = 3 * 24 * 3600.0
REAP_INTERVAL_SECONDS = 600.0


class IsolationRefused(RuntimeError):
    """Raised when the harness must not run a writer on the shared tree."""


def _now() -> float:
    return time.time()


def _ttl() -> float:
    raw = os.environ.get("YAAH_WORKTREE_TTL_SECONDS") or ""
    try:
        return float(raw) or DEFAULT_WORKTREE_TTL_SECONDS
    except ValueError:
        return DEFAULT_WORKTREE_TTL_SECONDS


def _component(value: str) -> str:
    """Branch/path-safe slug of one id component."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-.")
    return (slug or "x")[:60]


def branch_for(chat_id: str, run_id: str, label: str | None = None) -> str:
    """agent/<chat-id>/<label-slug>-<run-id>, or the legacy bare
    agent/<chat-id>/<run-id> when no label is known. The run uuid always
    survives intact at the end (anti-collision); the label is the human
    reading aid (users could not tell agent/221/f983bfef from any other)."""
    chat = _component(chat_id)
    run = _component(run_id)
    if not label:
        return f"{BRANCH_PREFIX}{chat}/{run}"
    # Readable lowercase slug: separators for spaces/underscores, everything
    # outside the git-safe alphabet dropped. Empty (e.g. a symbol-only
    # title) falls back to the bare form rather than a dangling dash.
    slug = re.sub(r"[^a-z0-9.-]+", "-", label.lower().replace("_", "-")).strip("-.")
    if not slug:
        return f"{BRANCH_PREFIX}{chat}/{run}"
    # One component, <=60 chars, run uuid never truncated: reserve exactly
    # len(run) + 1 (dash) of the budget for the suffix.
    keep = max(1, 60 - len(run) - 1)
    return f"{BRANCH_PREFIX}{chat}/{slug[:keep]}-{run}"


def worktree_base(root: Path) -> Path:
    return Path(root) / WT_PARENT / WT_DIRNAME


# ---------------------------------------------------------------------------
# sessions (what is currently bound where)
# ---------------------------------------------------------------------------

# wt_path -> {root, branch, chat_id, run_id, created}
_active: dict[str, dict] = {}
# chat_id -> wt_path (one binding per chat; try_begin_run guarantees a
# conversation runs at most one turn at a time)
_chat_bindings: dict[str, str] = {}
# Non-repo workspaces cannot host worktrees: status quo there is a shared
# tree, so exactly ONE writer is allowed at a time and a second concurrent
# writer is refused with a clear error (issue §1 non-repo fallback — never
# a silent fallthrough for concurrent writers).
_shared_writers: dict[str, set[str]] = {}  # workspace key -> tokens


def is_bound(chat_id: str) -> bool:
    return chat_id in _chat_bindings


def binding_for(chat_id: str) -> dict | None:
    wt = _chat_bindings.get(chat_id)
    return _active.get(wt) if wt else None


def worktree_of(workspace: str) -> str | None:
    """The worktree path when `workspace` IS a managed worktree, else None.

    Pure path-shape check (state-independent, survives restarts)."""
    try:
        p = Path(workspace).resolve()
    except OSError:
        return None
    if p.parent.name == WT_DIRNAME and p.parent.parent.name == WT_PARENT:
        return str(p)
    return None


def release_chat(chat_id: str) -> None:
    """Turn-end bookkeeping: drop a chat's non-repo shared-writer token.
    Bound worktree sessions are released by turn_end/release_session instead."""
    key = f"chat:{chat_id}"
    for holders in _shared_writers.values():
        holders.discard(key)


def session_rows() -> list[dict]:
    """Live sessions (for the reaper and future UI): a copy, never the
    internal dicts."""
    return [dict(v, path=k) for k, v in _active.items()]


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------



async def _git(
    cwd: Path | str, *args: str, timeout: float = 30.0, raw: bool = False
) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            _git_exe(),
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_NO_WINDOW,
            **_NEW_SESSION,
        )
    except OSError as e:
        return 128, f"git not runnable: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "git timed out"
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace")
        if raw
        else out.decode("utf-8", errors="replace").strip(),
    )


async def main_repo_root(workspace: str) -> Path | None:
    """The main repo root behind `workspace` (which may itself be a linked
    worktree), or None when it is not inside a git repo."""
    start = Path(workspace) if str(workspace).strip() else Path.home()
    try:
        start = start.resolve()
    except OSError:
        return None
    rc, out = await _git(start, "rev-parse", "--git-common-dir")
    if rc != 0:
        return None
    common = Path(out.strip())
    if not common.is_absolute():
        common = (start / common).resolve()
    root = common.parent.resolve()
    if root.name == ".git":  # degenerate layout guard
        root = root.parent
    rc, _ = await _git(root, "rev-parse", "--git-dir")
    return root if rc == 0 else None


async def _dirty(repo: Path | str) -> bool:
    rc, out = await _git(repo, "status", "--porcelain")
    return rc == 0 and bool(out.strip())


async def _dirty_paths(repo: Path | str) -> list[str]:
    """The uncommitted paths behind `_dirty`: tracked edits AND untracked
    files, parsed from `--porcelain -z` (NUL-delimited, so path text is
    exact — no column math against status letters, no quoting)."""
    rc, out = await _git(repo, "status", "--porcelain", "-z", raw=True)
    if rc != 0:
        return []
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, name = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            # rename/copy: the second path follows as its own NUL field —
            # skip it (the new path is already the one we captured above)
            i += 1
        if name:
            paths.append(name)
    return paths


async def _dirty_overlap(root: Path, branch: str, dirty: list[str]) -> list[str]:
    """Which uncommitted files this merge would need to update: the dirty
    paths intersected with the paths the branch changes relative to the
    merge base. Empty means git can merge around the dirt (its own rule:
    a merge only refuses when local changes collide with files it writes)."""
    rc, base = await _git(root, "merge-base", "HEAD", branch)
    spec = base.strip() if rc == 0 and base.strip() else "HEAD"
    rc, out = await _git(root, "diff", "--name-only", spec, branch)
    if rc != 0:
        return []  # cannot judge — git is the backstop at merge time
    touched = {line.strip() for line in out.splitlines() if line.strip()}
    return sorted(set(dirty) & touched)


def _parse_merge_veto_files(out: str) -> list[str]:
    """Extract the colliding paths from git's dirty-file veto output
    (issue #123). Only the tab-indented file list is accepted: the list
    ends at the first non-indented line (git's trailers — `Please
    commit...`, `Aborting`, `Updating ...` — and any further `error:`/
    `warning:` line are stderr prose, never entries), so a structured key
    never carries stderr-shaped junk. Git C-quotes non-ASCII paths
    (core.quotePath); those quotes are unwrapped and the octal escapes
    decoded back to the real path."""
    files: list[str] = []
    in_list = False
    for ln in (out or "").splitlines():
        if "would be overwritten by merge" in ln:
            in_list = True
            continue
        if not in_list:
            continue
        if not ln.startswith("\t") or not ln.strip():
            break  # first non-indented (or blank) line ends the list
        name = ln[1:].strip()
        if name.startswith('"') and name.endswith('"') and len(name) > 1:
            try:
                name = name[1:-1].encode("latin-1").decode("unicode_escape")
                name = name.encode("latin-1").decode("utf-8", "replace")
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
        if name and name not in files:
            files.append(name)
    return files


def _exclude_worktrees(root: Path) -> None:
    """R6 hygiene: hide .yaah/ via .git/info/exclude — never touch the
    user's tracked .gitignore (that edit would itself dirty the tree)."""
    try:
        info = root / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        text = (
            exclude.read_text(encoding="utf-8", errors="replace")
            if exclude.exists()
            else ""
        )
        if ".yaah/" not in text.splitlines():
            with open(exclude, "a", encoding="utf-8") as f:
                if text and not text.endswith("\n"):
                    f.write("\n")
                f.write(".yaah/\n")
    except OSError:
        pass  # cosmetic only


def _copy_env_files(src: Path, dst: Path) -> None:
    """Implementation note (issue): gitignored env files (.env et al.)
    don't exist in a fresh worktree — copy them so verification runs don't
    fail mysteriously."""
    try:
        for f in src.glob(".env*"):
            if f.is_file():
                shutil.copy2(f, dst / f.name)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# merge mutex (issue §3): in-process lock + lockfile with stale takeover
# ---------------------------------------------------------------------------

_mutex_locks: dict[str, asyncio.Lock] = {}


def _lockfile(root: Path) -> Path:
    return Path(root) / ".git" / "yaah-merge.lock"


async def _acquire_lockfile(lf: Path, deadline: float) -> None:
    while True:
        try:
            fd = os.open(lf, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            with contextlib.suppress(OSError):
                os.write(fd, f"pid={os.getpid()} t={_now():.0f}\n".encode())
            os.close(fd)
            return
        try:
            age = _now() - lf.stat().st_mtime
        except OSError:
            await asyncio.sleep(0.05)
            continue
        if age > STALE_LOCK_SECONDS:
            # crashed holder: merges are seconds-long, take over
            with contextlib.suppress(OSError):
                lf.unlink()
            continue
        if _now() > deadline:
            raise TimeoutError(
                "another YAAH process holds the merge lock on this workspace"
            )
        await asyncio.sleep(0.1)


@asynccontextmanager
async def merge_mutex(root: Path):
    """Serialize every merge into a shared tree (chat self-merges, parent
    merges, UI git ops — they are writers too, issue decision 3)."""
    key = str(root)
    lock = _mutex_locks.setdefault(key, asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), timeout=MUTEX_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as e:
        raise TimeoutError("merge mutex is busy (in-process)") from e
    try:
        lf = _lockfile(root)
        await _acquire_lockfile(lf, _now() + MUTEX_TIMEOUT_SECONDS)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                lf.unlink()
    finally:
        lock.release()


async def agent_branch_status(root: Path, branch: str) -> dict:
    """Read-only merged-ness probe for the UI's pendingMerge revalidation
    (issue #126): does the recorded agent/* branch still exist, and is it
    fully merged into the main-tree HEAD? Mirrors the exact `--merged`
    logic release_session and the reaper use — no deletion, no merge, no
    state change. A branch checked out in a live worktree still reports
    `merged` per git; the caller (UI) only suppresses the stale warning,
    the reaper's own hygiene is untouched."""
    root = Path(root)
    branch = str(branch or "").strip()
    if not branch:
        return {"exists": False, "merged_into_base": False}
    # Branch existence: git rev-parse --verify resolves both a branch name
    # and a full ref; ask for the branch ref specifically so a stray
    # commit-ish never masquerades as the recorded branch.
    rc, _ = await _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if rc != 0:
        return {"exists": False, "merged_into_base": False}
    rc, out = await _git(root, "branch", "--merged", "HEAD", "--list", branch)
    merged = rc == 0 and branch in out.split()
    return {"exists": True, "merged_into_base": merged}


# ---------------------------------------------------------------------------
# creation / rebinding (issue §1)
# ---------------------------------------------------------------------------


async def create_worktree(workspace: str, chat_id: str, run_id: str, label: str | None = None) -> dict:
    """Create `<main-root>/.yaah/worktrees/<chat-id>` on branch
    `agent/<chat-id>/<label-slug>-<run-id>` (or the bare run-id branch when
    no label), based on `workspace`'s current HEAD (a nested sub-agent's
    parent worktree is a valid base — that is the issue's nested fan-out).
    The directory is named by CHAT id (adr/0003): one worktree per chat
    session, so a restart can recover the binding from the path shape
    alone. `run_id` only names the branch (a re-created session after a
    root switch gets a fresh branch name, never a collision).
    Returns {ok: True, workspace, branch, root} or {ok: False, reason}."""
    root = await main_repo_root(workspace)
    if root is None:
        return {"ok": False, "reason": "not a git repository"}
    branch = branch_for(chat_id, run_id, label)
    # Capture the source branch before creating the isolated worktree. This
    # lets the UI distinguish the agent branch from the branch it was based on.
    base_rc, base_branch = await _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    base_branch = base_branch.strip() if base_rc == 0 else ""
    # Issue #115: remember who selected the branch. When the workspace the
    # session was started FROM is itself a managed session worktree, the
    # user explicitly picked its branch in the UI (e.g. `dev`) — record
    # that as the merge-back context so a later merge is confirmed against
    # it instead of silently landing on whatever the main tree shows.
    parent = worktree_of(workspace)
    if parent is not None:
        parent_info = _active.get(parent) or {}
        selected = str(parent_info.get("branch") or "")
        if selected and selected != base_branch:
            base_branch = f"{selected} (selected in {Path(parent).name})"
    wt = worktree_base(root) / _component(chat_id)
    rc, out = await _git(root, "worktree", "add", "-b", branch, str(wt))
    if rc != 0:
        reason = out or f"git worktree add exited {rc}"
        if "no commits" in reason or "does not have any commits" in reason:
            reason = "repository has no commits yet — nothing to branch from"
        return {"ok": False, "reason": reason}
    _exclude_worktrees(root)
    _copy_env_files(Path(workspace).resolve(), wt)
    info = {
        "root": str(root),
        "branch": branch,
        "base_branch": base_branch,
        "chat_id": chat_id,
        "run_id": run_id,
        "created": _now(),
    }
    _active[str(wt)] = info
    return {"ok": True, "workspace": str(wt), "branch": branch, "root": str(root)}


async def _chat_title(chat_id: str) -> str | None:
    """The pinned conversation's title, for human-readable agent branches.
    Best-effort by contract: ANY failure returns None and the branch falls
    back to the bare agent/<chat>/<run-id> form — a DB hiccup must never
    block a write tool call."""
    try:
        from backend.db.database import get_conversation

        conv = await get_conversation(int(chat_id))
        title = (conv or {}).get("title") or ""
        return title.strip() or None
    except Exception:  # noqa: BLE001
        return None


async def _branch_name(workspace: str) -> str:
    rc, branch = await _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    return branch.strip() if rc == 0 else ""


async def ensure_isolated(workspace: str, chat_id: str, on_lifecycle=None) -> str:
    """The rebinding seam: return the worktree path this caller must use,
    or the original workspace when isolation does not apply.

    Cheap state checks first (this runs before every write tool call):
    already-bound, already-a-worktree. Non-repo shared writers are capped
    at one concurrent writer (issue §1).

    adr/0003: the binding is a SESSION binding — once created it stays
    for the chat's lifetime (never released at turn end), so every turn
    of a chat works in the same tree. A binding recovered after a
    backend restart (path-shape discovery below) is re-registered here
    transparently."""
    ws = str(workspace)
    wt = worktree_of(ws)
    if wt is not None:
        if on_lifecycle:
            info = _active.get(wt, {})
            on_lifecycle({
                "event": "inherited", "workspace": wt,
                "branch": await _branch_name(wt),
                "base_branch": info.get("base_branch", ""),
            })
        return wt  # already a managed worktree (nested parent) — done
    if chat_id in _chat_bindings:
        bound = _chat_bindings[chat_id]
        # Root-match guard (adr/0003): the chat may have been refiled to
        # a different repo mid-session, or the bound directory may have
        # been removed underneath us (crashed teardown); either way the
        # binding is released (salvage-first) and a fresh session
        # worktree is created.
        try:
            bound_root = (
                await main_repo_root(bound)
                if Path(bound).exists()
                else None
            )
        except Exception:  # noqa: BLE001
            bound_root = None
        try:
            new_root = await main_repo_root(ws)
        except Exception:  # noqa: BLE001
            new_root = None
        if bound_root is not None and new_root is not None \
                and bound_root == new_root:
            if on_lifecycle:
                info = binding_for(chat_id) or {}
                on_lifecycle({
                    "event": "reused", "workspace": bound,
                    "branch": info.get("branch") or await _branch_name(bound),
                    "base_branch": info.get("base_branch", ""),
                })
            return bound
        await release_session(chat_id, why="workspace moved or session worktree gone")
    try:
        root = await main_repo_root(ws)
    except Exception:  # noqa: BLE001
        root = None
    if root is None:
        # Non-repo: status quo for the FIRST writer, refused for a second.
        key = str(Path(ws).resolve()) if ws.strip() else str(Path.home())
        holders = _shared_writers.setdefault(key, set())
        token = f"chat:{chat_id}"
        if holders and token not in holders:
            raise IsolationRefused(
                "another agent is already writing in this non-repo workspace; "
                "git worktree isolation is impossible here (issue #58)"
            )
        holders.add(token)
        return ws
    # Restart recovery (adr/0003): a session worktree from a previous
    # process lives on disk with the chat-id dir name but no in-memory
    # binding. Rebind to it instead of minting a second worktree for the
    # same chat — the session's uncommitted state survives the restart.
    candidate = worktree_base(root) / _component(chat_id)
    if candidate.is_dir() and worktree_of(str(candidate)) == str(candidate):
        rc, out = await _git(candidate, "rev-parse", "--abbrev-ref", "HEAD")
        if rc == 0 and out.strip().startswith(BRANCH_PREFIX):
            info = {
                "root": str(root),
                "branch": out.strip(),
                "chat_id": chat_id,
                "run_id": out.strip().rsplit("/", 1)[-1],
                "created": _now(),
                "recovered": True,
            }
            _active[str(candidate)] = info
            _chat_bindings[chat_id] = str(candidate)
            if on_lifecycle:
                on_lifecycle({
                    "event": "reused", "workspace": str(candidate),
                    "branch": info["branch"], "base_branch": info.get("base_branch", ""),
                })
            return str(candidate)
    run_id = uuid.uuid4().hex[:12]
    label: str | None = None
    if chat_id:
        label = await _chat_title(str(chat_id))
    created = await create_worktree(ws, chat_id or "chat", run_id, label=label)
    if not created.get("ok"):
        if on_lifecycle:
            on_lifecycle({"event": "create_failed", "reason": created.get("reason", "")})
        raise IsolationRefused(
            f"git worktree isolation refused: {created.get('reason')}"
        )
    wt = created["workspace"]
    _chat_bindings[chat_id] = wt
    if on_lifecycle:
        info = binding_for(chat_id) or {}
        on_lifecycle({
            "event": "created", "workspace": wt,
            "branch": created.get("branch", ""),
            "base_branch": info.get("base_branch", ""),
        })
    return wt


# ---------------------------------------------------------------------------
# merge-back (issue §2) — refusals are surfaced, never papered over
# ---------------------------------------------------------------------------


async def _new_commits(repo: Path, branch: str) -> int:
    rc, out = await _git(repo, "rev-list", "--count", f"HEAD..{branch}")
    if rc != 0:
        return -1
    try:
        return int(out.strip() or "0")
    except ValueError:
        return -1


def _rmtree_force(path: Path) -> bool:
    """rmtree that clears the read-only attribute git stamps on its files
    (Windows) and reports whether the tree is actually gone. A silent
    partial delete used to strand zombie worktrees the reaper re-salvaged
    every tick."""
    def _chmod_retry(func, target, _exc):
        with contextlib.suppress(OSError):
            os.chmod(target, stat.S_IWRITE)
            func(target)

    try:
        shutil.rmtree(path, onexc=_chmod_retry)
    except TypeError:  # py<3.12: onexc does not exist yet
        shutil.rmtree(path, onerror=_chmod_retry)
    return not path.exists()


async def _remove_worktree(root: Path, wt: Path, force: bool = False) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(wt))
    rc, _out = await _git(root, *args)
    if rc != 0 and not force:
        await _git(root, "worktree", "remove", "--force", str(wt))
    _active.pop(str(wt), None)
    if wt.exists():  # locked-file fallback (Windows): rmtree + prune stub
        _rmtree_force(wt)
        await _git(root, "worktree", "prune")


async def _salvage(root: Path, wt: Path, branch: str, why: str) -> str:
    """R4: never delete silently — capture ALL uncommitted work (tracked
    edits AND untracked files: `git diff HEAD` alone misses untracked
    content, which is exactly what a crashed run loses) before destroying
    anything."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = _component(branch.split("/")[-1])
    patch = worktree_base(root) / f"{slug}.{stamp}.salvage.patch"
    try:
        patch.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            await _git(wt, "add", "-A")  # stage untracked so they survive
        rc, diff = await _git(wt, "diff", "--cached", "HEAD")
        _rc2, staged_list = await _git(
            wt, "diff", "--cached", "--name-only", "HEAD"
        )
        with open(patch, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"# salvaged {branch} ({why}) at {stamp}\n")
            if staged_list:
                f.write(f"# salvaged files:\n# {staged_list}\n\n")
            f.write(diff or f"# (diff failed rc={rc})\n")
        return str(patch)
    except OSError:
        return ""




# Write provenance + trash classification live in provenance (ADR 0007
# ticket 2). Re-exported under the old names: callers (tools.py) and
# tests still import them from here.
from backend.agent.provenance import (  # noqa: F401
    provenance_classify as _provenance_classify,
    trash_class as _trash_class,
    is_machine_shape as _is_machine_shape,
    note_write,
    note_shell_writes,
    provenance_for,
    clear_provenance,
)


async def _drop_trash(wt: Path, rels: list[str]) -> list[str]:
    """Delete provably-generated uncommitted files so they cannot veto the
    merge. Deletion is the point (they are not work); failures are
    non-fatal (the file simply re-dirties the tree and the merge refuses
    as before)."""
    dropped: list[str] = []
    for rel in rels:
        try:
            (wt / rel).unlink()
            dropped.append(rel)
        except OSError:
            continue
    rc, _ = await _git(wt, "status", "--porcelain")
    return dropped if rc == 0 else []


async def _salvage_paths(
    root: Path, wt: Path, branch: str, why: str, rels: list[str]
) -> str:
    """Partial salvage: capture only `rels` to a patch before they are
    deleted. The insurance backstop for trash classifications that rest on
    shape heuristics alone (no tool provenance) — a false TRASH is the one
    deletion path whose content has no other copy."""
    if not rels:
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = _component(branch.split("/")[-1])
    patch = worktree_base(root) / f"{slug}.{stamp}.dropped-trash.patch"
    try:
        patch.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            await _git(wt, "add", "-A", "--", *rels)
        rc, diff = await _git(wt, "diff", "--cached", "HEAD", "--", *rels)
        with open(patch, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"# dropped-trash insurance {branch} ({why}) at {stamp}\n")
            f.write("# files:\n# " + ", ".join(rels) + "\n\n")
            f.write(diff or f"# (diff failed rc={rc})\n")
        # unstage: the drop that follows must leave the tree clean, or the
        # second contract pass re-processes the already-deleted paths
        with contextlib.suppress(Exception):
            await _git(wt, "reset", "-q", "--", *rels)
        return str(patch)
    except OSError:
        return ""


async def _drop_session_trash(
    wt: Path, root: Path, branch: str, why: str
) -> tuple[list[str], str]:
    """The adr/0002 session-end trash contract, shared by release_session
    and finalize_sub_agent: provably harness-generated uncommitted files
    (tool provenance) are dropped outright; shape-heuristic classifications
    without provenance get a patch first. Returns (dropped, insurance_patch)
    — insurance_patch is "" when every drop was provenance-backed."""
    dropped: list[str] = []
    insurance = ""
    for _pass in range(2):  # second pass re-checks after deletions
        paths = await _dirty_paths(wt)
        if not paths:
            break
        trash_map = _trash_class(paths, wt)
        trash = sorted(r for r, c in trash_map.items() if c == "trash")
        if not trash:
            break
        prov = _provenance_classify(wt, wt, trash)
        risky = [r for r in trash if prov.get(r) != "tool"]
        if risky:
            insurance = await _salvage_paths(root, wt, branch, why, risky)
        dropped = await _drop_trash(wt, trash)
        if not dropped:
            break  # unlink failed; git still sees them — salvage below
    return dropped, insurance


def live_session_branch(root: Path | str) -> str:
    """The branch of the live session bound to this main tree, or "".

    The merge refusal for a mistyped branch name (issue #103) carries
    this so the model can recover on the next call instead of
    misreporting the refusal as a failed merge. Bound session only —
    nothing is guessed from the path shape or the ref list.
    """
    try:
        root_key = Path(root).resolve()
    except OSError:
        root_key = Path(str(root))
    for wt_str in _chat_bindings.values():
        info = _active.get(wt_str)
        if not info:
            continue
        try:
            if Path(str(info.get("root", ""))).resolve() == root_key:
                return str(info.get("branch") or "")
        except OSError:
            continue
    return ""


def base_branch_for(root: Path | str, branch: str) -> str:
    """The branch context a merge-back should be confirmed against
    (issue #115): the base branch recorded for the session whose branch
    is `branch` — i.e. what the user had selected when the session
    started. Empty when there is no live binding for `branch`."""
    try:
        root_key = Path(root).resolve()
    except OSError:
        root_key = Path(str(root))
    for wt_str in _chat_bindings.values():
        info = _active.get(wt_str)
        if not info:
            continue
        try:
            if Path(str(info.get("root", ""))).resolve() != root_key:
                continue
        except OSError:
            continue
        if str(info.get("branch") or "") == branch:
            return str(info.get("base_branch") or "")
    return ""


async def resolve_session_branch(root: Path, branch: str) -> str | None:
    """Resolve an abbreviated `agent/*` branch name (issue #103).

    An exact `agent/*` ref wins untouched. A prefix like `agent/352` —
    the full slug is long and models abbreviate from memory — resolves
    only when exactly ONE `agent/*` branch starts with it plus a `/`;
    ambiguity or a miss returns None. Never a guess: merging the wrong
    agent branch is the one failure a merge cannot undo.
    """
    branch = (branch or "").strip()
    if not branch.startswith(BRANCH_PREFIX):
        return None
    rc, _ = await _git(root, "rev-parse", "--verify", branch)
    if rc == 0:
        return branch
    rc, out = await _git(
        root, "branch", "--list", "--format=%(refname:short)", f"{branch}/*"
    )
    if rc != 0:
        return None
    matches = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return matches[0] if len(matches) == 1 else None


async def merge_back(root: Path, branch: str, confirm: bool = False) -> dict:
    """Merge `branch` into the main tree under the merge mutex.

    Refuses (and says why) on: a branch with no commits beyond HEAD,
    uncommitted main-tree files the merge would overwrite (dirty overlap
    — never stash — issue decision 1), mid-merge state, and merge
    conflicts (aborted; the shared tree is left clean). Unrelated
    uncommitted work in the main tree does NOT block the merge: git's
    own overlap-aware pre-flight is the gate (docs/adr/0001).

    Issue #98 (docs/adr/0002): uncommitted files in the *worktree* are
    first classified — provably harness-generated output (provenance or
    machine shape) is dropped; authored-looking files keep the old
    refuse+salvage path via release_session.

    Issue #103: `branch` may be an abbreviation (e.g. `agent/352`) and
    resolves when exactly one `agent/*` branch matches; an empty name
    means this session's own branch.

    Issue #115 (decision: explicit confirmation when target differs):
    before executing, the resolved merge direction is stated. When the
    target branch the main tree currently has checked out differs from
    the session's base branch context (the branch the user had selected
    when the session started), the merge is NOT run on the first call —
    the payload returns `needs_confirmation` with the source→target
    preview, and the caller must surface that preview to the user and
    re-call with `confirm=True`. Plain `agent/*` sessions (whose base
    branch is the primary checkout's own) merge directly as before.
    """
    branch = (branch or "").strip()
    if not branch:
        session = live_session_branch(root)
        if not session:
            return {
                "merged": False,
                "reason": (
                    "no session worktree is bound to this workspace — "
                    "pass the agent branch to merge"
                ),
            }
        branch = session
    rc_head, _ = await _git(root, "rev-parse", "--verify", "HEAD")
    if rc_head != 0:
        return {"merged": False, "reason": "main tree has no commits to merge into"}
    rcv, _ = await _git(root, "rev-parse", "--verify", branch)
    if rcv != 0:
        resolved = await resolve_session_branch(root, branch)
        if resolved and resolved != branch:
            branch = resolved
        else:
            reason = f"branch {branch} does not exist"
            session = live_session_branch(root)
            if session:
                reason += (
                    f"; this session's branch is '{session}' — pass it verbatim"
                    " (or omit the branch argument to merge this session)"
                )
            return {"merged": False, "reason": reason}
    # Issue #115: state the merge direction before executing. When the
    # main tree's checked-out branch differs from this session's branch
    # context, the first call is a preview, not a merge.
    target_rc, target_branch = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    target = target_branch.strip() if target_rc == 0 else ""
    base = base_branch_for(root, branch)
    if not confirm and target and base and target != base:
        return {
            "merged": False,
            "needs_confirmation": True,
            "reason": (
                f"merge direction needs confirmation: '{branch}' \u2192 '{target}' "
                f"(this session's branch context is '{base}'). Surface this "
                "preview to the user and re-call with confirm=true to proceed"
            ),
            "source": branch,
            "target": target,
            "base_branch": base,
            "commits": count if (count := await _new_commits(root, branch)) else 0,
        }
    # Issue #115: state the merge direction before executing. When the
    # main tree's checked-out branch differs from this session's branch
    # context, the first call is a preview, not a merge.
    target_rc, target_branch = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    target = target_branch.strip() if target_rc == 0 else ""
    count = await _new_commits(root, branch)
    if count == 0:
        return {
            "merged": False,
            "reason": f"branch {branch} has no commits beyond HEAD",
            "zero_commits": True,
        }
    if count < 0:
        return {"merged": False, "reason": f"cannot inspect branch {branch}"}
    base = base_branch_for(root, branch)
    if not confirm and target and base and target != base:
        return {
            "merged": False,
            "needs_confirmation": True,
            "reason": (
                f"merge direction needs confirmation: '{branch}' \u2192 '{target}' "
                f"(this session's branch context is '{base}'). Surface this "
                "preview to the user and re-call with confirm=true to proceed"
            ),
            "source": branch,
            "target": target,
            "base_branch": base,
            "commits": count,
        }
    async with merge_mutex(root):
        if (root / ".git" / "MERGE_HEAD").exists():
            return {
                "merged": False,
                "reason": "main tree is mid-merge; resolve that merge first",
            }
        dirty_note = ""
        if await _dirty(root):
            dirty = await _dirty_paths(root)
            overlap = await _dirty_overlap(root, branch, dirty)
            if overlap:
                return {
                    "merged": False,
                    "reason": (
                        "main tree has uncommitted changes to file(s) this "
                        f"merge must update: {', '.join(overlap)} — commit "
                        "or stash them first; YAAH never stashes user work "
                        "to force a merge (issue #58 decision 1)"
                    ),
                    "dirty_overlap": overlap,
                }
            # Dirt exists but none of it collides with the merge: git's
            # own pre-flight is overlap-aware and would allow this merge,
            # so the dirt must not veto it (revisit of issue #58 decision
            # 1: blanket dirty refusals blocked every merge whenever the
            # human had unrelated WIP). If the human dirties a colliding
            # file while the merge runs, git refuses atomically and the
            # failure is surfaced below — never papered over, never a
            # stash. Untracked files are effectively untouched: an
            # untracked dirty path only lands in `overlap` when the
            # branch modifies that same path, which git also refuses on
            # ("untracked working tree files would be overwritten").
            dirty_note = (
                f"merged around {len(dirty)} unrelated uncommitted "
                "file(s) in the main tree"
            )
        rc, out = await _git(root, "merge", "--no-edit", branch, timeout=120)
        if rc != 0:
            with contextlib.suppress(Exception):
                await _git(root, "merge", "--abort")
            # git's own refusal ("local changes ... would be overwritten")
            # means the human dirtied a colliding file mid-merge — a veto,
            # not a content conflict. Surface git's message naming the file.
            refused = "would be overwritten" in (out or "")
            result = {
                "merged": False,
                "conflict": not refused,
                "reason": out or "merge failed (aborted)",
            }
            if refused:
                # Issue #123: surface git's file list structurally so the
                # agent can escalate to the user without parsing stderr.
                # Issue #124 review: same key as the pre-flight payload
                # (`dirty_overlap`) — one concept, one name.
                blocked = _parse_merge_veto_files(out or "")
                if blocked:
                    result["dirty_overlap"] = blocked
                    result["reason"] = (
                        "main tree has uncommitted changes to file(s) this "
                        f"merge must update: {', '.join(blocked)} — commit "
                        "or stash them first; YAAH never stashes user work "
                        "to force a merge (issue #58 decision 1)"
                    )
            return result
    from backend.agent import gitinfo

    gitinfo.invalidate_git_caches(root)
    result: dict = {
        "merged": True,
        "commits": count,
        "branch": branch,
        "session_branch": live_session_branch(root),
        "target_branch": target,
    }
    if dirty_note:
        result["note"] = dirty_note
    return result


async def ff_session_branch(wt_str: str) -> None:
    """Fast-forward a session worktree's branch to the main tree's current
    HEAD (adr/0003): after a successful merge-back the session branch must
    equal what the user's folder shows, or the next turn starts behind.
    Best-effort: a dirty worktree file that collides with the ff leaves
    the branch where it is (the next turn's merge handles it)."""
    info = _active.get(str(wt_str))
    if info is None:
        return
    root = Path(info["root"])
    rc, main_head = await _git(root, "rev-parse", "HEAD")
    if rc != 0 or not main_head.strip():
        return
    with contextlib.suppress(Exception):
        await _git(Path(wt_str), "merge", "--ff-only", "-q", main_head.strip())


async def turn_end(chat_id: str) -> dict:
    """End-of-turn settlement for a top-level chat agent (adr/0003 revised:
    branch-first — the harness never merges into the main tree on its own).

    - Zero commits AND clean worktree: nothing to preserve — the session is
      drained (release_session: dir + binding + zero-commit branch gone)
      and the next turn runs on the main tree, re-isolating on demand.
    - Otherwise the session stays bound: commits stay on the session
      branch (master untouched — merging is the user's deliberate
      decision via git_merge_back or plain git) and uncommitted state
      survives into the next turn.
    - Best-effort ff keeps a worktree current with the main tree; it is
      fast-forward only, so a branch carrying its own commits never moves.

    Returns {branch, commits_ahead, dirty, drained} for the UI's honest
    chip/status."""
    info = binding_for(chat_id)
    wt_str = _chat_bindings.get(chat_id, "")
    if info is None:
        release_chat(chat_id)
        return {"drained": False, "noop": True, "reason": "turn was not isolated"}
    root = Path(info["root"])
    wt = Path(wt_str)
    branch = info["branch"]

    commits = await _new_commits(root, branch)
    if commits <= 0:
        # No commits on the branch: the session may be drainable. The
        # adr/0002 trash contract runs here because draining IS a session
        # end — a stray command capture must not pin a read-only chat's
        # session forever.
        await _drop_session_trash(wt, root, branch, "turn-end drain check")
        if not await _dirty(wt):
            await release_session(chat_id, why="drained (branch carried no work)")
            return {
                "drained": True,
                "branch": branch,
                "base_branch": info.get("base_branch", ""),
                "worktree_id": chat_id,
                "worktree": wt_str,
                "commits_ahead": 0,
                "dirty": False,
                "worktree_removed": True,
            }
    await ff_session_branch(wt_str)
    return {
        "drained": False,
        "branch": branch,
        "base_branch": info.get("base_branch", ""),
        "worktree_id": chat_id,
        "worktree": wt_str,
        "commits_ahead": max(commits, 0),
        "dirty": bool(await _dirty(wt)),
    }


# ---------------------------------------------------------------------------
# Domain operations (issue #58 seam deepening): callers bind before a write
# and settle at turn end through these two functions instead of assembling
# lifecycle semantics from primitives. Results carry pre-interpreted events,
# notes, and status so the loop stays an emitter, not a policy owner.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindResult:
    """Outcome of bind_for_write — everything the caller needs to keep the
    UI chip, the model context, and the run state honest, already resolved.

    workspace: the path tools must run in after this call (unchanged when
    no binding was needed). refires_only: True means the binding already
    existed for this chat and no fresh bind happened — do not re-emit
    worktree_bound / append a second note (a fresh binding that actually
    rebinds away from the main tree is the only event-worthy case).
    required: False means this tool did not require a session worktree and
    workspace is unchanged. When required is True, reused distinguishes an
    existing chat binding from a fresh one. Refusals ride on IsolationRefused.
    """
    workspace: str
    required: bool
    reused: bool
    worktree: str
    branch: str
    base_branch: str
    worktree_id: str
    model_note: str
    lifecycle: dict  # raw event, recorded to git activity by the caller


@dataclass(frozen=True)
class SettleResult:
    """Outcome of settle_session — the turn-end interpretation of turn_end().

    drained=True means the session was quiesced and released (worktree
    removed, chip reverts to the main tree). Otherwise branch/commits_ahead/
    dirty describe the surviving session. status_note is the honest-status
    payload for worktree_status persistence/events, or None when there is
    nothing to report (no commits ahead, no drain)."""
    drained: bool
    branch: str
    base_branch: str
    worktree_id: str
    worktree: str
    commits_ahead: int
    dirty: bool
    worktree_removed: bool
    status_note: dict | None


async def _session_note(chat_id: str) -> dict:
    """The binding info a fresh (non-reused) bind must surface."""
    return binding_for(str(chat_id)) or {}


def _worktree_note_text(wt_path: str, info: dict, main_workspace: str) -> str:
    branch = info.get("branch", "")
    main_root = info.get("root") or main_workspace
    return (
        "# Workspace integration\n\n"
        f"Your isolated working copy is `{wt_path}` on `{branch}`; the main "
        f"workspace is `{main_root}`. This is implementation plumbing: treat "
        "the main workspace as the user's task target and do not ask them to "
        "manage checkouts or branches.\n"
        f"- Your session branch is exactly `{branch}`. `git_merge_back` takes "
        "this exact string, or no branch argument at all to merge this "
        "session. Never reconstruct or abbreviate the name from memory; the "
        "main tree cannot be un-merged.\n"
        "- For requested code changes, verify proportionately, commit when "
        "needed, and integrate with `git_merge_back` before reporting complete. "
        "Turn end itself never merges. Never say work is in main until the "
        "merge succeeds.\n"
        "- If integration fails, never stash or overwrite user work. First "
        "classify the outcome: dirty overlap (name the user's changed paths), "
        "content conflict (name the conflicted paths and confirm the merge "
        "was aborted), or another refusal (state the exact reason and inspect "
        "both trees). Then offer safe options with trade-offs, such as resolve "
        "on the isolated branch and retry, leave it isolated, or have the user "
        "resolve specific main-workspace edits. Explain what changed and what "
        "did not before asking how to proceed; never claim unmerged work is "
        "in main.\n"
        "- ESCALATION CONTRACT (issue #123): a `merged: false` refusal is a "
        "decision point, not a retry loop. On a dirty-overlap refusal the "
        "payload names the colliding files in `dirty_overlap`) "
        "\u2014 use that list and ask the user exactly once via `ask_user` "
        "(commit it / discard it and merge / leave the merge isolated). "
        "Re-running identical diagnostics (git status, log, branch, worktree "
        "list) more than 2 times against the same refusal state is a hard "
        "stop; do not start new issues or side-quests while a merge-back "
        "refusal is unresolved unless the user explicitly parks it.\n"
        "- For structured git_status, git_diff, git_pull, and git_push, choose "
        "target=current or target=main explicitly. Use main only when the user "
        "explicitly asks about or operates on the primary checkout; clarify "
        "if ambiguous. Main "
        "pulls are clean-tree, upstream-checked, and fast-forward-only. For a "
        "primary-branch push, preserve requested order, verify branch, upstream, "
        "and clean status, then use target=main. Never force-push; stop on "
        "unexpected state or non-fast-forward and verify the remote ref.\n"
        "# Worktree lifecycle\n\n"
        "Fact sheet about this worktree \u2014 reason from these facts, never from "
        "timestamps or branch names alone:\n"
        "- Created lazily at this session's first write-capable call; read-only "
        "turns never pay for isolation.\n"
        "- If it disappears between turns, the isolation plumbing recreates it "
        "(fresh `agent/*` branch) \u2014 that is plumbing churn, not the reaper.\n"
        "- Turn end does NOT tear it down while it holds commits or authored "
        "dirt; uncommitted files persist across turns intact. Only a fully "
        "quiesced session (no commits, clean tree) is drained at turn end.\n"
        "- The reaper only sweeps ORPHANED worktrees (no live session) idle "
        ">48h (`YAAH_WORKTREE_TTL_SECONDS`; zero-commit `agent/*` branches 3d) "
        "on a 10-min sweep. A session you are actively using is never reaped "
        "mid-conversation, and unmerged `agent/*` branches are never "
        "auto-pruned \u2014 the branch is the record of the work.\n"
        "- Never 'clean up' a worktree you did not create this turn \u2014 other "
        "concurrent chats may own it; deleting one can destroy their work."
    )


async def bind_for_write(
    workspace: str,
    chat_id: str,
    *,
    tool_name: str | None = None,
    args: dict | None = None,
    child: bool = False,
    note: str | None = None,
    on_lifecycle=None,
) -> BindResult:
    """Bind this conversation to its session worktree before a write
    (issue #58 / adr/0003).

    One operation for the loop's gate and approval paths (and the
    sub-agent runner): decides whether this tool needs a session worktree,
    reuses or creates the binding when required, and returns everything the
    caller must surface — the rebound workspace, lifecycle event to record, and
    the model note for a FRESH binding. Raises IsolationRefused when
    isolation cannot happen (the caller turns that into the tool's
    error result — never a dead turn).

    note: an optional extra line appended to the fresh-bind note (used by
    the sub-agent runner to carry its own context line).
    """
    required = (
        tool_name is None or should_isolate(tool_name, args or {}, child=child)
    )
    if not required:
        return BindResult(
            workspace=str(workspace),
            required=False,
            reused=False,
            worktree="",
            branch="",
            base_branch="",
            worktree_id=str(chat_id),
            model_note="",
            lifecycle={},
        )
    wt_path = await ensure_isolated(workspace, chat_id, on_lifecycle=on_lifecycle)
    fresh = wt_path != str(workspace)
    info = await _session_note(chat_id) if fresh else {}
    lifecycle = dict(info) if fresh else {}
    return BindResult(
        workspace=wt_path,
        required=True,
        reused=not fresh,
        worktree=wt_path if fresh else "",
        branch=str(info.get("branch") or ""),
        base_branch=str(info.get("base_branch") or ""),
        worktree_id=str(chat_id),
        model_note=(
            _worktree_note_text(wt_path, info, workspace)
            + (f"\n{note}" if note else "")
            if fresh
            else ""
        ),
        lifecycle=lifecycle,
    )


async def settle_session(chat_id: str) -> SettleResult:
    """Settle/release the session at turn end (adr/0003 revised).

    Interpretation of turn_end(): drains a quiesced session, otherwise
    reports the surviving branch. Returns the git-activity context facts
    and the honest-status note (None when there is nothing to persist —
    the loop emits worktree_status only when work exists on the branch).
    """
    settle = await turn_end(chat_id)
    if settle.get("noop"):
        return SettleResult(
            drained=False, branch="", base_branch="", worktree_id=chat_id,
            worktree="", commits_ahead=0, dirty=False, worktree_removed=False,
            status_note=None,
        )
    drained = bool(settle.get("drained"))
    status_note = None
    if drained:
        status_note = None
    elif settle.get("commits_ahead"):
        status_note = {
            "branch": str(settle.get("branch") or ""),
            "base_branch": str(settle.get("base_branch") or ""),
            "worktree_id": str(settle.get("worktree_id") or chat_id),
            "worktree": str(settle.get("worktree") or ""),
            "commits": int(settle.get("commits_ahead") or 0),
            "dirty": bool(settle.get("dirty")),
            "worktree_removed": bool(settle.get("worktree_removed")),
        }
    return SettleResult(
        drained=drained,
        branch=str(settle.get("branch") or ""),
        base_branch=str(settle.get("base_branch") or ""),
        worktree_id=str(settle.get("worktree_id") or chat_id),
        worktree=str(settle.get("worktree") or ""),
        commits_ahead=int(settle.get("commits_ahead") or 0),
        dirty=bool(settle.get("dirty")),
        worktree_removed=bool(settle.get("worktree_removed")),
        status_note=status_note,
    )


async def release_session(chat_id: str, why: str = "session ended") -> dict:
    """Session end (adr/0003): chat deleted, workspace refiled, or the
    reaper collecting an orphan. Terminal teardown of the chat's session
    worktree — the adr/0002 trash contract runs HERE, once: provably
    harness-generated uncommitted files are dropped, authored-looking
    leftovers are salvaged to a patch (never silently deleted).

    Physical teardown (worktree directory removal, branch deletion) is
    NOT done here: the salvage patch is only a diff against the branch,
    and a salvage heuristic that misclassifies authored work as trash is
    unrecoverable once the tree is gone. Instead the released worktree is
    left in place (binding cleared, so the reaper sees an orphan) and the
    reaper performs the teardown after the worktree TTL (48h default) —
    plenty of time to notice a bad salvage and recover from the intact
    tree. A released session whose chat comes back is re-adopted by
    ensure_isolated's restart-recovery (same dir, same branch), so a
    lingering worktree is recovered state, not stranded state."""
    info = binding_for(chat_id)
    wt_str = _chat_bindings.get(chat_id, "")
    if info is None:
        release_chat(chat_id)
        return {"released": False, "noop": True, "reason": "no session worktree"}
    root = Path(info["root"])
    wt = Path(wt_str)
    branch = info["branch"]

    def _unbind() -> None:
        _chat_bindings.pop(chat_id, None)
        _active.pop(wt_str, None)
        clear_provenance(wt_str)
        release_chat(chat_id)

    dropped, insurance = await _drop_session_trash(
        wt, root, branch, f"session end ({why})"
    )

    salvage_note = ""
    if await _dirty(wt):
        patch = await _salvage(root, wt, branch, f"session end ({why})")
        salvage_note = (
            f"uncommitted changes salvaged to {patch or '(salvage failed)'}"
        )
    commits = await _new_commits(root, branch)
    _unbind()
    # Recovery over tidiness (worktree reaper owns deletion): the trash
    # contract above already captured anything droppable to patches, but
    # the physical tree and the branch stay until the reaper's TTL —
    # EXCEPT a truly quiesced session (clean tree after the trash drop,
    # zero commits): removal there is lossless by the deletion test, and
    # a drained read-only chat must not pin a dir for two days.
    quiesced = commits <= 0 and not await _dirty(wt)
    if quiesced:
        await _remove_worktree(root, wt, force=True)
        # With the tree gone the branch is unchecked-out: a zero-commit
        # branch is deleted outright (nothing to lose); otherwise git's
        # exact merged-ness decides (-d refuses an unmerged branch).
        if commits <= 0:
            with contextlib.suppress(Exception):
                await _git(root, "branch", "-D", branch)
            merged_note = "branch deleted (zero commits)"
        else:
            rc, out = await _git(
                root, "branch", "--merged", "HEAD", "--list", branch
            )
            if rc == 0 and branch in out.split():
                with contextlib.suppress(Exception):
                    await _git(root, "branch", "-d", branch)
                merged_note = "branch fully merged into HEAD: deleted"
            else:
                merged_note = "branch retained (not fully merged)"
    else:
        # The dir stays (reaper removes it after the TTL), so the branch
        # is checked out and MUST survive — deletion here would fail
        # anyway. The reaper deletes it after teardown, exact-merged only.
        merged_note = (
            "worktree kept for the reaper's TTL cleanup "
            "(branch retained: checked out in the kept worktree)"
        )
    out: dict = {"released": True, "branch": branch, "worktree": str(wt)}
    if quiesced:
        out["drained"] = True
        merged_note = "quiesced session: worktree removed (nothing to preserve); " + merged_note
    if dropped:
        out["dropped_trash"] = dropped
    if insurance:
        out["note"] = (
            f"heuristic-classified file(s) dropped; contents backed up to "
            f"{insurance}"
        )
    if salvage_note:
        out["note"] = (
            (out.get("note") + "; " if out.get("note") else "") + salvage_note
        )
    out["note"] = (
        (out.get("note") + "; " if out.get("note") else "") + merged_note
    )
    return out


async def finalize_sub_agent(agent_workspace: str, result: dict) -> dict:
    """Sub-agents never merge (issue §2): pin the branch onto the first
    line of the final message, salvage uncommitted work instead of
    dropping it, remove the worktree directory, and keep the branch for
    the parent to merge."""
    info = _active.get(agent_workspace)
    if info is None:
        return result
    root = Path(info["root"])
    wt = Path(agent_workspace)
    branch = info["branch"]
    _active.pop(agent_workspace, None)
    # adr/0003 hygiene: a sub-agent binds under its own chat key (the
    # spawn call id); finalize is its session end — the binding must go,
    # or the in-memory map leaks an entry per sub-agent run.
    _chat_bindings.pop(info.get("chat_id", ""), None)

    commits = await _new_commits(root, branch)
    dropped, insurance = await _drop_session_trash(
        wt, root, branch, "sub-agent completion"
    )
    note = ""
    if insurance:
        note = (
            "heuristic-classified file(s) were dropped; contents backed up "
            f"to {insurance}. "
        )
    dirty = await _dirty(wt)
    if dirty:
        patch = await _salvage(
            root, wt, branch, "uncommitted changes at sub-agent completion"
        )
        note += (
            "worktree had uncommitted changes; they are NOT on the branch. "
            f"Diff salvaged to {patch or '(salvage failed)'}"
        )
    elif commits == 0:
        note = "no commits were made on the worktree branch"
    await _remove_worktree(root, wt, force=bool(dropped) or bool(await _dirty(wt)))
    if dropped:
        note = (
            f"{len(dropped)} generated file(s) dropped at finalize: "
            + ", ".join(dropped[:5])
            + ("; " if note else "")
            + note
        )

    output = str(result.get("output") or "")
    if commits > 0:
        first = (
            f"[changes are on branch `{branch}` ({commits} commit(s)); merge "
            f"it into your own tree before relying on them]"
        )
    else:
        first = f"[worktree branch `{branch}` carries no commits]"
    result["output"] = f"{first}\n\n{output}" if output else first
    if note:
        result["worktree_note"] = note
    result["worktree_branch"] = branch
    result["commits_ahead"] = max(commits, 0)
    result["dirty"] = dirty
    result["worktree_removed"] = True
    return result


# ---------------------------------------------------------------------------
# background main-tree sync (issue #98 / adr/0002): the visible folder never
# sits silently behind upstream
# ---------------------------------------------------------------------------

SYNC_INTERVAL_SECONDS = 900.0
_sync_task: asyncio.Task | None = None
_sync_busy = asyncio.Lock()


async def sync_main_trees() -> list[dict]:
    """Fast-forward every registered main tree that sits strictly behind
    its upstream. Safety stack: ff-only (a diverged tree is never touched),
    git's overlap-aware working-tree guard (the user's uncommitted files —
    tracked or untracked — are never overwritten; colliding paths fail the
    ff and are left exactly as they were), and the merge mutex (never race
    a live turn's merge-back; a busy tree is skipped, the next tick
    retries). Per-workspace failures are non-fatal by contract."""
    try:
        from backend.db.database import list_workspaces
    except ImportError:
        return []
    try:
        workspaces = await list_workspaces()
    except Exception:  # noqa: BLE001 — a DB hiccup must not crash the sync
        return []
    synced: list[dict] = []
    for ws in workspaces:
        path = (ws or {}).get("path") or ""
        if not path.strip():
            continue
        try:
            root = await main_repo_root(path)
        except Exception:  # noqa: BLE001
            continue
        if root is None:
            continue
        async with merge_mutex(root):
            gitdir = root / ".git"
            if (
                (gitdir / "MERGE_HEAD").exists()
                or (gitdir / "rebase-merge").exists()
                or (gitdir / "rebase-apply").exists()
            ):
                continue  # mid-merge or mid-rebase — never touch it
            rc, out = await _git(root, "rev-parse", "--abbrev-ref", "@{u}")
            if rc != 0:
                continue  # no upstream configured — nothing to sync to
            upstream = out.strip()
            rc, ahead_behind = await _git(
                root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}"
            )
            if rc != 0:
                continue
            parts = ahead_behind.split()
            if len(parts) != 2 or parts[0] != "0" or parts[1] == "0":
                # diverged, in sync, or unparsable — ff-only means hands off
                continue
            rc, _out = await _git(root, "merge", "--ff-only", upstream, timeout=60)
            if rc == 0:
                from backend.agent import gitinfo

                gitinfo.invalidate_git_caches(root)
                synced.append({"workspace": str(root), "fast_forwarded": True})
    return synced


async def _sync_loop() -> None:
    # Short first delay: startup (DB init, MCP spawn) is still settling;
    # the sync must never compete with the boot path.
    await asyncio.sleep(20.0)
    while True:
        if not _sync_busy.locked():
            async with _sync_busy:
                with contextlib.suppress(Exception):
                    await sync_main_trees()
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


def start_background_sync() -> None:
    global _sync_task
    if _sync_task is None or _sync_task.done():
        _sync_task = asyncio.create_task(_sync_loop())


def stop_background_sync() -> None:
    global _sync_task
    if _sync_task is not None:
        _sync_task.cancel()
        _sync_task = None


# ---------------------------------------------------------------------------
# reaper (issue §2 mid-batch cancellation + TTL cleanup)
# ---------------------------------------------------------------------------


# Worktrees whose removal failed (locked files): already salvaged once;
# re-salvaging every tick would grow patch litter unboundedly. Retry only
# the removal until something (a reboot, a process exit) unlocks the tree.
_reap_failed: set[str] = set()


async def reap_stale(now: float | None = None) -> dict:
    """Salvage-before-delete for orphaned worktrees (crashed runs, aborted
    batches, restarts): anything under .yaah/worktrees that is not a live
    session and older than the TTL gets the session-end teardown
    (adr/0003: trash dropped, authored leftovers salvaged, directory
    removed, zero-commit branch deleted) instead of a bare salvage.
    `agent/*` branches past the branch TTL are pruned (decision 4), except
    branches of live sessions (a >TTL session with no commits yet must not
    lose its branch)."""
    now = _now() if now is None else now
    ttl = _ttl()
    reaped, salvaged = [], []
    live_paths = set(_chat_bindings.values())
    live_branches = {
        info.get("branch") for info in _active.values() if info.get("branch")
    }

    roots: set[Path] = set()
    for wt_str, info in list(_active.items()):
        if wt_str not in live_paths and now - info.get("created", now) > ttl:
            roots.add(Path(info["root"]))
    # discover leftovers on disk even after a restart (info lost): roots
    # come from every registered workspace (DB), not just _active — a
    # RELEASED session (release_session leaves the dir for the reaper)
    # and a crashed run have no _active entry at all.
    with contextlib.suppress(Exception):
        from backend.db.database import list_workspaces

        for ws in await list_workspaces() or []:
            path = ws.get("path") if isinstance(ws, dict) else None
            if path and Path(path).is_dir():
                roots.add(Path(path))
    # any root already referenced by a live binding (covers tests and
    # embedded runs where the DB may be empty)
    for info in list(_active.values()):
        roots.add(Path(info["root"]))

    for root in list(roots):
        wt_base = worktree_base(root)
        if not wt_base.is_dir():
            continue
        for child in sorted(wt_base.iterdir()):
            if not child.is_dir():
                continue
            if str(child) in live_paths:
                continue
            if str(child) in _active and now - _active[str(child)].get("created", now) <= ttl:
                continue
            info = _active.get(str(child), {})
            branch = info.get("branch") or ""
            if not branch:
                # unknown session (restart): recover branch from git
                rc, out = await _git(child, "rev-parse", "--abbrev-ref", "HEAD")
                branch = (
                    out.strip()
                    if rc == 0 and out.strip().startswith(BRANCH_PREFIX)
                    else child.name
                )
            if await _dirty(child) and str(child) not in _reap_failed:
                patch = await _salvage(root, child, branch, "TTL reaper")
                if patch:
                    salvaged.append(patch)
            await _remove_worktree(root, child, force=True)
            if child.exists():
                # removal failed (locked tree) — do not re-salvage next tick
                _reap_failed.add(str(child))
                continue
            _reap_failed.discard(str(child))
            # adr/0003 branch hygiene: an orphan whose branch is fully
            # merged (or that never had any) is deleted, not kept for
            # three days. Exact merged-ness, same as release_session —
            # never a commits-beyond-HEAD approximation.
            rc, out = await _git(root, "rev-parse", "--verify", branch)
            if rc != 0:
                pass  # branch gone already
            else:
                rc, out = await _git(
                    root, "branch", "--merged", "HEAD", "--list", branch
                )
                if rc == 0 and branch in out.split():
                    with contextlib.suppress(Exception):
                        await _git(root, "branch", "-d", branch)
            reaped.append(str(child))
        # Branch pruning: the 3-day TTL now applies only to branches that
        # are fully merged (merged or zero-commit litter). Under
        # the branch-first contract an unmerged agent/* branch IS the
        # record of the work — the user deletes it, never the reaper.
        rc, out = await _git(
            root,
            "for-each-ref",
            "--format=%(refname:short) %(committerdate:unix)",
            "refs/heads/agent",
        )
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) != 2:
                    continue
                name, date_s = parts
                try:
                    date = float(date_s)
                except ValueError:
                    continue
                if now - date > BRANCH_TTL_SECONDS and name not in live_branches:
                    rc, out = await _git(
                        root, "branch", "--merged", "HEAD", "--list", name
                    )
                    if rc == 0 and name in out.split():
                        await _git(root, "branch", "-d", name)
    return {"reaped": reaped, "salvaged": salvaged}


_task: asyncio.Task | None = None


async def _loop() -> None:
    while True:
        await asyncio.sleep(REAP_INTERVAL_SECONDS)
        with contextlib.suppress(Exception):
            await reap_stale()


def start_reaper() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def stop_reaper() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None


# ---------------------------------------------------------------------------
# UI plumbing
# ---------------------------------------------------------------------------


def filter_agent_branches(branches: list[str]) -> list[str]:
    """Keep `agent/*` branches out of the user's branch dropdown (issue
    hygiene: they are merge-back artifacts, not checkout targets)."""
    return [b for b in branches if not b.startswith(BRANCH_PREFIX)]


# ---------------------------------------------------------------------------
# sync convenience (tests + one-off admin)
# ---------------------------------------------------------------------------


def run_sync_git(cwd: Path | str, *args: str) -> subprocess.CompletedProcess:
    """Blocking git run through the same executable resolution as the
    async path. Used by tests to seed fixtures on real repos."""
    return subprocess.run(
        [_git_exe(), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
