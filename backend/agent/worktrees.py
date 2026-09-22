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

Layout: worktrees live at `<main-root>/.yaah/worktrees/<run-id>` on
branches `agent/<chat-id>/<run-id>`. The path is excluded via
`.git/info/exclude` (never the user's tracked .gitignore), branches are
filtered out of the UI branch list, and search prunes the `.yaah`
directory (see tools.IGNORED_DIRS).

Merge rules (issue decisions 1-6):
- Sub-agents never merge: they report the branch on the first line of
  their final message (results are clipped; the parent must always be
  able to act on the branch name).
- Top-level chat agents self-merge at end of turn under the mutex.
- Merge-back refuses a dirty worktree, zero new commits, uncommitted
  main-tree files that the merge would overwrite (dirty *overlap* —
  never stash), mid-merge state, and merge conflicts (aborting) —
  surfaced, never papered over. Unrelated WIP does not block a merge:
  git's own overlap-aware pre-flight decides (see docs/adr/0001).
- The worktree directory is deleted after merge-back; the `agent/*`
  branch is kept a few days (the reaper prunes it).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
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


def branch_for(chat_id: str, run_id: str) -> str:
    return f"{BRANCH_PREFIX}{_component(chat_id)}/{_component(run_id)}"


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
    Bound worktree sessions are released by self_merge instead."""
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


async def create_worktree(workspace: str, chat_id: str, run_id: str) -> dict:
    """Create `<main-root>/.yaah/worktrees/<run-id>` on branch
    `agent/<chat-id>/<run-id>`, based on `workspace`'s current HEAD (a
    nested sub-agent's parent worktree is a valid base — that is the
    issue's nested fan-out). Returns {ok: True, workspace, branch, root}
    or {ok: False, reason}."""
    root = await main_repo_root(workspace)
    if root is None:
        return {"ok": False, "reason": "not a git repository"}
    branch = branch_for(chat_id, run_id)
    wt = worktree_base(root) / _component(run_id)
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
        "chat_id": chat_id,
        "run_id": run_id,
        "created": _now(),
    }
    _active[str(wt)] = info
    return {"ok": True, "workspace": str(wt), "branch": branch, "root": str(root)}


async def ensure_isolated(workspace: str, chat_id: str) -> str:
    """The rebinding seam: return the worktree path this caller must use,
    or the original workspace when isolation does not apply.

    Cheap state checks first (this runs before every write tool call):
    already-bound, already-a-worktree. Non-repo shared writers are capped
    at one concurrent writer (issue §1)."""
    ws = str(workspace)
    wt = worktree_of(ws)
    if wt is not None:
        return wt  # already a managed worktree (nested parent) — done
    if chat_id in _chat_bindings:
        return _chat_bindings[chat_id]
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
    run_id = uuid.uuid4().hex[:12]
    created = await create_worktree(ws, chat_id or "chat", run_id)
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
        shutil.rmtree(wt, ignore_errors=True)
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


async def merge_back(root: Path, branch: str) -> dict:
    """Merge `branch` into the main tree under the merge mutex.

    Refuses (and says why) on: a branch with no commits beyond HEAD,
    uncommitted main-tree files the merge would overwrite (dirty overlap
    — never stash — issue decision 1), mid-merge state, and merge
    conflicts (aborted; the shared tree is left clean). Unrelated
    uncommitted work in the main tree does NOT block the merge: git's
    own overlap-aware pre-flight is the gate (docs/adr/0001), and a
    successful merge around unrelated dirt is noted in the result.
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


async def self_merge(chat_id: str) -> dict:
    """End-of-turn merge for a top-level chat agent (issue decision 5).
    No-op when the turn never got isolated. The worktree directory is
    removed on success; on refusal it stays (with the branch) so nothing
    is lost and the refusal can be acted on."""
    info = binding_for(chat_id)
    wt_str = _chat_bindings.pop(chat_id, "")
    if info is None:
        release_chat(chat_id)
        return {"merged": False, "noop": True, "reason": "turn was not isolated"}
    root = Path(info["root"])
    wt = Path(wt_str)
    branch = info["branch"]
    try:
        if await _dirty(wt):
            patch = await _salvage(root, wt, branch, "uncommitted changes at self-merge")
            await _remove_worktree(root, wt, force=True)
            return {
                "merged": False,
                "reason": (
                    f"merge-back refused: this turn left uncommitted changes in "
                    f"its worktree; they are NOT in the main tree. Branch "
                    f"{branch} kept; diff salvaged to {patch or '(salvage failed)'}"
                ),
            }
        result = await merge_back(root, branch)
        if result.get("merged"):
            await _remove_worktree(root, wt)
        return result
    finally:
        _active.pop(wt_str, None)
        release_chat(chat_id)


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

    commits = await _new_commits(root, branch)
    dirty = await _dirty(wt)
    note = ""
    if dirty:
        patch = await _salvage(
            root, wt, branch, "uncommitted changes at sub-agent completion"
        )
        note = (
            "worktree had uncommitted changes; they are NOT on the branch. "
            f"Diff salvaged to {patch or '(salvage failed)'}"
        )
    elif commits == 0:
        note = "no commits were made on the worktree branch"
    await _remove_worktree(root, wt, force=dirty)

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
# reaper (issue §2 mid-batch cancellation + TTL cleanup)
# ---------------------------------------------------------------------------


async def reap_stale(now: float | None = None) -> dict:
    """Salvage-before-delete for orphaned worktrees (crashed runs, aborted
    batches, restarts): anything under .yaah/worktrees that is not a live
    session and older than the TTL gets its diff salvaged and the
    directory removed. `agent/*` branches past the branch TTL are pruned
    (decision 4)."""
    now = _now() if now is None else now
    ttl = _ttl()
    reaped, salvaged = [], []
    live_paths = set(_chat_bindings.values())

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
            if await _dirty(child):
                patch = await _salvage(root, child, branch, "TTL reaper")
                if patch:
                    salvaged.append(patch)
            await _remove_worktree(root, child, force=True)
            reaped.append(str(child))
        # branch pruning (decision 4: keep a few days, then remove)
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
                if now - date > BRANCH_TTL_SECONDS:
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
