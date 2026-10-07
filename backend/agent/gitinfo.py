"""Git workspace info for the UI (current branch, dirty state, commit
positions), kept out of the tool layer on purpose: this is app state, not a
model tool.

Cheap by design — stat() the .git/HEAD file and only spawn git when it
changed. The chat panel re-polls the endpoint every couple of seconds, so a
terminal `git checkout` reflects in the UI without any push channel, and a
non-repo workspace costs one failed stat per poll.
"""
import asyncio
import os
import time
from pathlib import Path

from backend.agent import gitexec
from backend.agent.remote import parse_ns

# Windows: suppress the console window a console child of the windowed app
# would pop up. POSIX: own process group (mirrors tools.py).
_SUBPROCESS_FLAGS = (
    {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
)

# (resolved workspace root) -> (mtime_ns captured at last read, branch text)
_cache: dict[str, tuple[float, str | None]] = {}

# #333: remote roots have no HEAD to stat, so their branch-cache entries
# are time-gated instead of mtime-gated (keyed by monotonic capture time).
_cache_times: dict[str, float] = {}
_BRANCH_TTL = 2.0  # seconds; matches the UI poll cadence

# (resolved workspace root) -> (monotonic time captured, info dict). One git
# spawn burst per TTL per workspace no matter how many pollers ask — the
# status strip's readouts (branch, divergence, line counts) must describe the
# same instant, so they are computed together and cached together.
_info_cache: dict[str, tuple[float, dict]] = {}
_INFO_TTL = 2.0  # seconds; matches the UI poll cadence
_GIT_TIMEOUT = 5.0


def _head_path(root: Path) -> Path | None:
    """The file whose mtime tracks branch switches."""
    git = root / ".git"
    if git.is_file():
        # Worktrees/submodules: .git is a stub file "gitdir: <path>".
        try:
            text = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = text.split(":", 1)[1].strip()
            p = Path(target)
            if not p.is_absolute():
                p = root / target
            return p / "HEAD"
        return None
    if git.is_dir():
        return git / "HEAD"
    return None


def is_git_repo(root: Path | str) -> bool:
    """True when the directory looks like a git repo or worktree checkout
    (a .git dir or stub file). Pure stat — no spawn. #302: the staleness
    checks guard with this, because list_local_branches returns [] for a
    non-repo (git fails), which must not read as 'branch deleted'."""
    return _head_path(Path(root)) is not None


async def current_git_branch(root: Path | str) -> str | None:
    """Current branch name, or None when the workspace is not a git repo
    (detached HEADs report the short SHA).

    #333: routed through the git gateway. Local workspaces keep the
    HEAD-mtime fast path; remote workspaces ask the host through the
    channel (TTL-cached below — a remote poll costs a round-trip)."""
    root = Path(root)
    ws = str(root)
    remote = parse_ns(ws) is not None

    head = None if remote else _head_path(root)
    if not remote and head is None:
        return None
    mtime = 0
    if head is not None:
        try:
            mtime = head.stat().st_mtime_ns
        except OSError:
            return None

    key = ws
    cached = _cache.get(key)
    if cached:
        if not remote and cached[0] == mtime:
            return cached[1]
        if remote and time.monotonic() - _cache_times.get(key, 0.0) < _BRANCH_TTL:
            return cached[1]

    branch: str | None = None
    if remote:
        # #333: the gateway merges stderr at the source (the host's shell
        # redirects 2>&1), so the ambiguity-poisoning case is guarded
        # structurally: a branch name is a single line — trust the output
        # only when git left nothing else (warnings) behind.
        res = await gitexec.run_git(ws, "rev-parse", "--abbrev-ref", "HEAD")
        if res is not None:
            rc, out = res
            if rc == 0 and out and "\n" not in out:
                branch = out
    else:
        # _run_git (which passes --no-optional-locks, issue #279). stderr is
        # discarded for this lookup only: a workspace with a local branch named
        # HEAD makes rev-parse emit an ambiguity warning on stderr while still
        # exiting 0, and merged output would poison the branch value.
        rc, out = await _run_git(
            root, "rev-parse", "--abbrev-ref", "HEAD", merge_stderr=False
        )
        if rc == 0:
            branch = out or None

    # Cache keyed on the observed mtime (local) or capture time (remote):
    # when HEAD changes, the mtime mismatch forces a re-read.
    if remote:
        _cache[key] = (0.0, branch)
        _cache_times[key] = time.monotonic()
    else:
        _cache[key] = (mtime, branch)
    return branch


# ------------------------------------------------------------- ui readout

async def _run_git(
    root: Path, *args: str, merge_stderr: bool = True
) -> tuple[int, str]:
    """One git invocation in the workspace; (returncode, combined output
    unless merge_stderr is False, which discards stderr).

    Always passes --no-optional-locks: this layer is a read-only UI poll, and
    taking (or blocking on) index.lock/HEAD.lock would make its 2 s burst
    contend with the agent's own git writes — a bursty stall that shows up as
    input-path latency (issue #279).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "--no-optional-locks", "-C", str(root), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=(
                asyncio.subprocess.STDOUT
                if merge_stderr
                else asyncio.subprocess.DEVNULL
            ),
            **_SUBPROCESS_FLAGS,
        )
    except OSError:
        return 127, "git not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "git timed out"
    return proc.returncode, out.decode("utf-8", errors="replace").strip()


def _invalidate_branch_cache(root: Path | str) -> None:
    """Drop the HEAD-mtime cache entry so the next branch poll re-reads."""
    _cache.pop(str(Path(root)), None)


async def git_workspace_info(root: Path | str) -> dict | None:
    """Everything the status strip's git readouts need, in one git burst.

    Returns None when the workspace is not a git repo. Shape:
      branch            current branch name (detached HEAD -> short SHA)
      local_hash        7-char short hash of HEAD, or None
      remote_hash       7-char hash of the upstream ref, None = no upstream
      ahead, behind     commit counts vs upstream (0/0 when no upstream)
      added, deleted    net diff lines vs HEAD (tracked changes only)
      dirty             worktree has any change (incl. untracked files)
      untracked         count of untracked files (tooltip detail)
    TTL-cached: the strip's readouts must describe one instant, and the UI
    polls every ~2s, so the cache lives 2s.
    """
    root = Path(root)
    key = str(root)
    remote = parse_ns(key) is not None
    now = time.monotonic()
    cached = _info_cache.get(key)
    if cached and now - cached[0] < _INFO_TTL:
        return cached[1]

    # #333: an unreachable remote host is an explicit state, never a silent
    # None — the strip renders "host offline" instead of vanishing. Local
    # non-repos keep today's None (not-a-repo is data, not unreachability).
    if remote:
        res = await gitexec.run_git(key, "status", "-sb", "--porcelain")
        if res is None:
            info = {"offline": True}
            _info_cache[key] = (now, info)
            return info
        return _info_from_status(out=res[1], branch=None, key=key, now=now)

    branch = await current_git_branch(root)
    if branch is None:
        _info_cache[key] = (now, None)
        return None

    # rev-list --left-right --count gives ahead/behind in one call;
    # numstat gives added/deleted lines. `git diff` on an unborn branch
    # (no commits yet) fails — fall back to zeroed numbers.
    head_ok, head_ref = await _run_git(root, "rev-parse", "HEAD")
    if head_ok == 0:
        head_ref = head_ref.strip()
    else:
        head_ref = None

    upstream: str | None = None
    if head_ref:
        rc, out = await _run_git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        if rc == 0:
            upstream = out.strip() or None
    ahead = behind = 0
    if upstream and head_ref:
        rc, out = await _run_git(
            root, "rev-list", "--left-right", "--count", f"{upstream}...HEAD"
        )
        if rc == 0 and out:
            parts = out.split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])

    local_hash = remote_hash = None
    if head_ref:
        rc, out = await _run_git(root, "rev-parse", "--short=7", "HEAD")
        if rc == 0:
            local_hash = out.strip() or None
    if upstream:
        rc, out = await _run_git(root, "rev-parse", "--short=7", upstream)
        if rc == 0:
            remote_hash = out.strip() or None

    added = deleted = 0
    untracked = 0
    if head_ref:
        rc, out = await _run_git(root, "diff", "--numstat", "HEAD")
        if rc == 0:
            for line in out.splitlines():
                if not line.strip():
                    continue
                # _run_git merges stderr in, so warning lines (e.g. CRLF
                # notices) land here tab-less; they are not numstat rows.
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                a, d, _p = parts
                # Binary files report "-" for both counts; ignore them.
                added += int(a) if a.isdigit() else 0
                deleted += int(d) if d.isdigit() else 0
    rc, out = await _run_git(root, "ls-files", "--others", "--exclude-standard")
    if rc == 0:
        untracked = sum(1 for line in out.splitlines() if line.strip())

    rc, out = await _run_git(root, "status", "--porcelain")
    dirty = rc == 0 and bool(out.strip())
    changed = sum(1 for line in out.splitlines() if line.strip()) if rc == 0 else 0

    info = {
        "branch": branch,
        "upstream": upstream,
        "local_hash": local_hash,
        "remote_hash": remote_hash,
        "ahead": ahead,
        "behind": behind,
        "added": added,
        "deleted": deleted,
        "dirty": dirty,
        "untracked": untracked,
        "changed": changed,
    }
    _info_cache[key] = (now, info)
    return info


def _info_from_status(
    out: str, branch: str | None, key: str, now: float
) -> dict:
    """#333: the remote readout, from one `git status -sb --porcelain`
    burst (cross-dialect safe, one round-trip). Shape matches the local
    info dict; counts git cannot express stay 0 rather than wrong."""
    branch = None
    upstream: str | None = None
    ahead = behind = 0
    lines = out.splitlines()
    if lines:
        head = lines[0]
        if head.startswith("##"):
            head = head[2:].strip()
            # Forms: branch...upstream [ahead N, behind M] | branch | HEAD
            # (detached, short SHA) | branch (no upstream).
            if "..." in head:
                b, _, rest = head.partition("...")
                branch = b.strip() or None
                rest, _, _trail = rest.partition(" [")
                upstream = rest.strip() or None
                marker = out.split(" [", 1)[1].rsplit("]", 1)[0] if " [" in out else ""
                for part in marker.split(","):
                    part = part.strip()
                    if part.startswith("ahead"):
                        ahead = _int_or_zero(part[5:])
                    elif part.startswith("behind"):
                        behind = _int_or_zero(part[6:])
            else:
                branch = head.strip() or None
    dirty = any(
        line.strip() and not line.startswith("## ") for line in lines
    )
    return {
        "branch": branch,
        "upstream": upstream,
        "local_hash": None,
        "remote_hash": None,
        "ahead": ahead,
        "behind": behind,
        "added": 0,
        "deleted": 0,
        "dirty": dirty,
        "untracked": sum(
            1 for line in lines if line.startswith("?? ")
        ),
        "changed": sum(
            1 for line in lines if line.strip() and not line.startswith("##")
        ),
    }


def _int_or_zero(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return 0


_branch_list_cache: dict[str, tuple[float, list[str]]] = {}
_BRANCH_LIST_TTL = 2.0  # seconds; matches the info-readout cache cadence


async def list_local_branches(root: Path | str) -> list[str]:
    """Local branch names for the chip's dropdown (current branch included;
    sorted by git's default ordering). Empty when not a repo / unborn HEAD.

    #302: TTL-cached — the chip's staleness read rides this list every
    ~2s poll, and a per-poll spawn would break the git-branch endpoint's
    cheap-by-design contract. Callers that must see fresh state after a
    branch write go through invalidate_git_caches, which drops it."""
    root = Path(root)
    key = str(root)
    now = time.monotonic()
    cached = _branch_list_cache.get(key)
    if cached and now - cached[0] < _BRANCH_LIST_TTL:
        return cached[1]
    res = await gitexec.run_git(root, "branch", "--format=%(refname:short)")
    if res is None:
        return []
    rc, out = res
    if rc != 0:
        return []
    branches = [line.strip() for line in out.splitlines() if line.strip()]
    _branch_list_cache[key] = (now, branches)
    return branches


def invalidate_git_caches(root: Path | str) -> None:
    """Force the next info/branch poll to re-read from git (after a UI-driven
    checkout, commit, push, pull ...)."""
    root = Path(root)
    _invalidate_branch_cache(root)
    _cache_times.pop(str(root), None)  # #333: remote branch-TTL bookkeeping
    _info_cache.pop(str(root), None)
    _branch_list_cache.pop(str(root), None)  # #302: staleness reads re-read
