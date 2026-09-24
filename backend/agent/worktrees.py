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
  session (adr/0003): the first write-capable tool call creates it, and
  it is never released mid-session. The harness NEVER merges into the
  main tree on its own: commits stay on the session branch until the
  user merges deliberately (git_merge_back, or plain git). The old
  per-turn auto-merge hid the work in a phantom branch while the UI
  claimed master — and made a follow-up "push" publish the wrong ref.
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
- The worktree directory is deleted at session end; the `agent/*`
  branch is deleted with it when it carries no unmerged commits
  (already-merged or zero-commit sessions leave no branch litter), and
  kept for inspection a few days otherwise (the reaper prunes it).

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
from pathlib import Path

# Wire-in triggers (issue #58 §1): a write-capable tool call before which
# the caller's workspace must be rebound to a worktree. git_add/git_commit
# are deliberately NOT triggers — by the time an agent can commit it must
# already be isolated (write access can only have come from one of these).
WRITER_TRIGGERS = {"bash", "powershell", "write_file", "edit_file", "create_file"}

# A shell call is only a writer when its command actually mutates: repo
# inspection and sync (status/diff/fetch/pull/push) run on the main tree —
# a plain "pull from origin" must move the user's branch, not mint an
# agent/<chat> worktree for it. The classifier only gates the FIRST
# binding: once a chat is bound, every later call runs in the session
# worktree regardless. Fail-closed: anything unrecognized isolates.
_READONLY_GIT = {
    "status", "log", "diff", "show", "branch", "remote", "rev-parse",
    "tag", "fetch", "pull", "push",
}
_READONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "pwd", "rg", "grep", "find", "wc",
    "which", "where", "dir", "type", "echo",
}

_SHELL_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")


def _readonly_segment(seg: str) -> bool:
    """One shell pipeline stage: recognized read-only, or env/cd prefixes
    in front of one. Anything else (unknown binary, flags that could hide
    a write, subshells) fails closed."""
    tokens = seg.strip().split()
    while tokens:
        first = tokens[0]
        if first in ("env", "time") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", first):
            tokens = tokens[1:]
            continue
        if first == "cd":
            return False
        break
    if not tokens:
        return False
    if tokens[0] == "git":
        return len(tokens) > 1 and tokens[1] in _READONLY_GIT
    if tokens[0] in _READONLY_COMMANDS:
        # Version probes like `node --version` and bare `ls` are fine;
        # don't try to whitelist every flag combination of every tool.
        return True
    if len(tokens) == 2 and tokens[1] in ("--version", "-v", "--help", "-h"):
        return True
    return False


def _readonly_shell_command(command: str) -> bool:
    if ">" in command or "<" in command or "$(" in command or "`" in command:
        return False
    return all(
        _readonly_segment(seg) or not seg.strip()
        for seg in _SHELL_SPLIT_RE.split(command)
    )


def should_isolate(tool_name: str, args: dict) -> bool:
    """Whether this tool call must run isolated. True for the file writers
    and powershell (no per-command grammar); for bash, decided by the
    command itself — read-only commands stay on the main tree. The
    git_pull/git_push tools are the structured form of `git pull/push`,
    so they follow the same repo-sync rule (a plain "pull from origin"
    must move the user's branch, not mint a session worktree)."""
    if tool_name in ("git_pull", "git_push"):
        return False
    if tool_name != "bash":
        return True
    command = str((args or {}).get("command") or "").strip()
    if not command:
        return True  # no command to vouch for — fail closed
    return not _readonly_shell_command(command)


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
# forever, but a slow verification run must survive (default 6h,
# YAAH_WORKTREE_TTL_SECONDS overrides). Branches: kept for inspection a
# few days (issue decision 4), then pruned.
DEFAULT_WORKTREE_TTL_SECONDS = 6 * 3600.0
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

_GIT_EXE: str | None = None


def _git_exe() -> str:
    """A directly-spawnable git executable. `shutil.which('git')` normally
    lands on a real .exe; hosts that only expose a .cmd shim (dev sandboxes)
    need the fallback probe — CreateProcess cannot exec a .cmd."""
    global _GIT_EXE
    if _GIT_EXE:
        return _GIT_EXE
    found = shutil.which("git")
    if found and found.lower().endswith(".exe"):
        _GIT_EXE = found
        return _GIT_EXE
    import sys

    candidates = [
        Path(Path(sys.executable).anchor) / "Program Files" / "Git" / "cmd" / "git.exe",
        Path.home() / "Desktop" / "toolkit" / "mingit" / "cmd" / "git.exe",
        Path.home() / "scoop" / "apps" / "git" / "current" / "cmd" / "git.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            _GIT_EXE = str(cand)
            return _GIT_EXE
    _GIT_EXE = found or "git"  # last resort; the spawn error will explain
    return _GIT_EXE


async def _git(
    cwd: Path | str, *args: str, timeout: float = 30.0, raw: bool = False
) -> tuple[int, str]:
    from backend.agent.tools import _NEW_SESSION, _NO_WINDOW

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


async def ensure_isolated(workspace: str, chat_id: str) -> str:
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
            return str(candidate)
    run_id = uuid.uuid4().hex[:12]
    label: str | None = None
    if chat_id:
        label = await _chat_title(str(chat_id))
    created = await create_worktree(ws, chat_id or "chat", run_id, label=label)
    if not created.get("ok"):
        raise IsolationRefused(
            f"git worktree isolation refused: {created.get('reason')}"
        )
    wt = created["workspace"]
    _chat_bindings[chat_id] = wt
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


# ---------------------------------------------------------------------------
# write provenance (issue #98 / docs/adr/0002): what did this turn WRITE?
#
# The classifier below can only refuse-or-drop a file when it can tell WORK
# from TRASH. Tools report what they wrote here as they write it; the
# harness seeds the registry before a write tool call and clears it when
# the worktree is torn down. Two classes:
#   model — content the model authored (write_file/create_file/edit_file
#           payloads): real work, treated as precious.
#   tool  — harness-generated captures (bash/powershell output redirect
#           captures written by the harness): trash unless the model then
#           edited the same path itself.
# Provenance is a hint, never a veto: an unknown path (say, an `npm install`
# that wrote package-lock.json) falls through to the shape heuristics, and
# the user's own files in the main tree are untouched by all of this.
# ---------------------------------------------------------------------------

_WRITE_PROVENANCE: dict[str, dict[str, set[str]]] = {}
# workspace -> {"model": {relpath, ...}, "tool": {relpath, ...}}

# Windows/POSIX path normalization for registry keys: forward slashes,
# lowercased drive, so `C:\x\y` and `c:/x/y` are one entry.
def _norm_path(p: str | Path) -> str:
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = s[0].upper() + s[1:]
    return s


def note_write(workspace: str, path: str, kind: str) -> None:
    """Record that a tool wrote `path` in `workspace` (kind: model|tool)."""
    if kind not in ("model", "tool"):
        return
    ws = _WRITE_PROVENANCE.setdefault(_norm_path(workspace), {})
    ws.setdefault(kind, set()).add(_norm_path(path))


def provenance_for(workspace: str, path: str) -> str | None:
    """'model' | 'tool' when this turn wrote the path, else None.

    Accepts the path relative to `workspace` or absolute; both registered
    forms are checked, and MODEL always wins (a capture the model later
    edited is authored work, not a capture)."""
    ws = _WRITE_PROVENANCE.get(_norm_path(workspace))
    if not ws:
        return None
    np = _norm_path(path)
    cands = {np}
    try:
        cands.add(_norm_path(Path(workspace) / path))
    except OSError:
        pass
    if any(c in ws.get("model", ()) for c in cands):
        return "model"
    if any(c in ws.get("tool", ()) for c in cands):
        return "tool"
    return None


def clear_provenance(workspace: str) -> None:
    _WRITE_PROVENANCE.pop(_norm_path(workspace), None)


# Shell output redirections: `cmd > f`, `cmd >> f`, `cmd 2> f`, `cmd &> f`,
# `cmd 2>&1 > f`, and the PowerShell twins `Out-File f` / `> f`. The harness
# itself writes these capture files when a command's stdout is redirected
# into the workspace — content the model never authored — so they register
# as `tool` provenance and the classifier can drop them at merge time.
_REDIRECT_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\d?\s*>+|\d?&>|&>)\s*([^\s|&;<>]+)"
    r"|(?:^|[\s;&|(])out-file\s+(?:-\w+\s+)*([^\s|&;<>-]+)",
    re.IGNORECASE,
)


def note_shell_writes(workspace: str, command: str) -> None:
    """Register redirection targets in `command` as tool-written paths.
    Best-effort by contract: parsing is heuristic (quotes, subshells and
    expansions are not interpreted); a miss just means the file falls back
    to the shape heuristics."""
    for m in _REDIRECT_RE.finditer(command or ""):
        target = m.group(1) or m.group(2)
        if target:
            note_write(workspace, target, "tool")


def _provenance_classify(workspace: str, wt: Path, paths: list[str]) -> dict[str, str]:
    """provenance class per dirty path: model > tool > unknown."""
    out: dict[str, str] = {}
    for rel in paths:
        cls = provenance_for(wt, rel) or provenance_for(wt, wt / rel)
        out[rel] = cls or "unknown"
    return out


# Machine-shape fingerprints (adr/0002): content that only a build tool,
# test runner, or redirect produces. Conservative by design — a miss just
# means the file takes the precious path (salvage + refusal), exactly the
# pre-#98 behavior; a false TRASH is the only dangerous direction, so the
# checks are structural, not name-based wishful thinking.
_TRASH_EXTS = {
    ".log", ".tmp", ".temp", ".swp", ".swo", ".pyc", ".pyo",
    ".patch", ".rej", ".orig", ".bak", ".salvage", ".out",
    # .txt (adr/0002): in a WORKTREE, uncommitted .txt is overwhelmingly a
    # command capture or tool dump — authored text goes through
    # write_file/create_file (provenance `model`, protected regardless).
    # This is the one knowingly-imperfect extension: an agent-authored
    # .txt that arrived via a parse-missed redirect would be dropped
    # (residual risk accepted; the content also lives in the transcript).
    ".txt",
}

_TRASH_NAMES = {"npm-debug.log", "yarn-error.log", "yarn.lock.check", ".DS_Store"}

# Extensions a redirect capture plausibly uses (adr/0002): tool-provenance
# files with these extensions are captures, not authored documents. Authored
# extensions (.md, code files) keep the precious path even when the model
# created them via a redirect — `gh pr view 96 > notes.md` is intent.
_CAPTURE_EXTS = {".txt", ".json", ".csv", ".tsv", ".ndjson", ".xml", ".yaml", ".yml"}


def _is_machine_shape(p: Path) -> bool:
    """Structural fingerprints of generated output. All byte sniffing is
    capped (32 KB head / 4 KB tail) — this runs per dirty file at merge
    time and must stay cheap."""
    try:
        if not p.is_file():
            return False
        with p.open("rb") as f:
            head = f.read(32768)
            if p.stat().st_size > 4096:
                f.seek(-4096, 2)  # tail window, relative to EOF
            tail = f.read(4096)
    except OSError:
        return False
    if not head:
        return False
    # diff/patch walls: git salvage patches, compiler error dumps
    stripped = head.lstrip()
    if any(
        stripped.startswith(sig)
        for sig in (b"diff ", b"--- ", b"+++ ", b"@@ -", b"Index:")
    ):
        return True
    # unified-diff body (our salvage patches carry a comment header first)
    if b"\ndiff --git " in head and b"\n+++" in head:
        return True
    # base64 wall: saved crash dumps / image captures dropped as text
    dense = sum(1 for ch in head if 48 <= ch <= 122)
    if len(head) >= 1024 and dense / len(head) > 0.97:
        return True
    # JSON object/array with a parsed balanced-bracket budget: build
    # manifests, test-output envelopes, tsbuildinfo — but NOT a hand-written
    # config the model may have authored (those are short; the size gate
    # plus the depth requirement keeps them out of TRASH).
    body = head.strip()
    if body[:1] in (b"{", b"[") and len(body) > 256:
        depth = curly = 0
        in_str = False
        esc = False
        for ch in body:
            byte = ch.to_bytes(1, "big")
            if esc:
                esc = False
            elif byte == b"\\":
                esc = True
            elif byte == b'"':
                in_str = not in_str
            elif not in_str:
                if byte == b"{":
                    curly += 1
                    depth = max(depth, curly)
                elif byte == b"}":
                    curly -= 1
        if depth >= 2 and curly == 0:
            return True
    # log-ish tail: line after line of timestamps/levels/severities
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if len(lines) >= 4:
        logish = sum(
            1
            for ln in lines
            if ln[:1].isdigit()
            or ln[:1] == b"["
            or b"ERROR" in ln
            or b"DEBUG" in ln
            or b"WARNING" in ln
        )
        if logish / len(lines) >= 0.75:
            return True
    return False


def _trash_class(worktree_dirty: list[str], wt: Path) -> dict[str, str]:
    """Classify each dirty path: 'work' (precious) or 'trash' (droppable).

    A path is TRASH only when provenance says harness-generated, or when it
    carries a machine-shape fingerprint (extension, name, or content shape).
    Everything else — anything authored-looking, anything uncertain — is
    WORK, which takes the old refuse+salvage path. (adr/0002: only a false
    'trash' can lose work, so uncertainty always lands on WORK.)
    """
    classes = _provenance_classify(wt, wt, worktree_dirty)
    out: dict[str, str] = {}
    for rel, cls in classes.items():
        if cls == "model":
            out[rel] = "work"
            continue
        p = wt / rel
        ext = p.suffix.lower()
        if cls == "tool":
            # The harness captured it — trash when the extension says
            # capture (a redirected .md / code file stays precious: the
            # model aimed output at a real artifact).
            if ext in _CAPTURE_EXTS or ext in _TRASH_EXTS:
                out[rel] = "trash"
                continue
            out[rel] = "work"
            continue
        if ext in _TRASH_EXTS or p.name in _TRASH_NAMES:
            out[rel] = "trash"
            continue
        if _is_machine_shape(p):
            out[rel] = "trash"
            continue
        out[rel] = "work"
    return out


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


async def merge_back(root: Path, branch: str) -> dict:
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
    """
    rc_head, _ = await _git(root, "rev-parse", "--verify", "HEAD")
    if rc_head != 0:
        return {"merged": False, "reason": "main tree has no commits to merge into"}
    rcv, _ = await _git(root, "rev-parse", "--verify", branch)
    if rcv != 0:
        return {"merged": False, "reason": f"branch {branch} does not exist"}
    async with merge_mutex(root):
        if (root / ".git" / "MERGE_HEAD").exists():
            return {
                "merged": False,
                "reason": "main tree is mid-merge; resolve that merge first",
            }
        count = await _new_commits(root, branch)
        if count == 0:
            return {
                "merged": False,
                "reason": f"branch {branch} has no commits beyond HEAD",
                "zero_commits": True,
            }
        if count < 0:
            return {"merged": False, "reason": f"cannot inspect branch {branch}"}
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
            return {
                "merged": False,
                "conflict": not refused,
                "reason": out or "merge failed (aborted)",
            }
    from backend.agent import gitinfo

    gitinfo.invalidate_git_caches(root)
    result: dict = {"merged": True, "commits": count, "branch": branch}
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


async def release_session(chat_id: str, why: str = "session ended") -> dict:
    """Session end (adr/0003): chat deleted, workspace refiled, or the
    reaper collecting an orphan. Terminal teardown of the chat's session
    worktree — the adr/0002 trash contract runs HERE, once: provably
    harness-generated uncommitted files are dropped, authored-looking
    leftovers are salvaged to a patch (never silently deleted), the
    worktree directory is removed, and the branch is deleted when it
    carries no unmerged commits (merged or zero-commit sessions leave no
    branch litter). A branch with unmerged commits is kept for
    inspection (the reaper prunes it after the branch TTL)."""
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
    await _remove_worktree(root, wt, force=True)
    _unbind()
    # Branch hygiene (adr/0003): a session branch whose commits are all
    # merged (or that never had any) is deleted with the worktree — one
    # read-only chat must not litter agent/* for three days. A branch
    # with unmerged commits is kept for inspection (reaper prunes later).
    if commits <= 0:
        with contextlib.suppress(Exception):
            await _git(root, "branch", "-D", branch)
    out: dict = {"released": True, "branch": branch}
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
    if await _dirty(wt):
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
    # discover leftovers on disk even after a restart (info lost):
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
            # adr/0003 branch hygiene: an orphan whose branch carries no
            # unmerged commits is deleted, not kept for three days.
            if await _new_commits(root, branch) <= 0:
                with contextlib.suppress(Exception):
                    await _git(root, "branch", "-D", branch)
            reaped.append(str(child))
        # Branch pruning: the 3-day TTL now applies only to branches that
        # carry NO unmerged commits (merged or zero-commit litter). Under
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
                    if await _new_commits(root, name) <= 0:
                        await _git(root, "branch", "-D", name)
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
