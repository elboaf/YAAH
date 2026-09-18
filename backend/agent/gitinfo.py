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

# Windows: suppress the console window a console child of the windowed app
# would pop up. POSIX: own process group (mirrors tools.py).
_SUBPROCESS_FLAGS = (
    {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
)

# (resolved workspace root) -> (mtime_ns captured at last read, branch text)
_cache: dict[str, tuple[float, str | None]] = {}

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


async def current_git_branch(root: Path | str) -> str | None:
    """Current branch name, or None when the workspace is not a git repo
    (detached HEADs report the short SHA)."""
    root = Path(root)
    head = _head_path(root)
    if head is None:
        return None
    try:
        mtime = head.stat().st_mtime_ns
    except OSError:
        return None

    key = str(root)
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]

    branch: str | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **_SUBPROCESS_FLAGS,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            out = b""
        if proc.returncode == 0:
            text = out.decode("utf-8", errors="replace").strip()
            branch = text or None
    except (OSError, ValueError):
        branch = None

    # Cache keyed on the observed mtime: when HEAD changes, the mtime
    # mismatch forces a re-read.
    _cache[key] = (mtime, branch)
    return branch


# ------------------------------------------------------------- ui readout

async def _run_git(root: Path, *args: str) -> tuple[int, str]:
    """One git invocation in the workspace; (returncode, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(root), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
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
    now = time.monotonic()
    cached = _info_cache.get(key)
    if cached and now - cached[0] < _INFO_TTL:
        return cached[1]

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


async def list_local_branches(root: Path | str) -> list[str]:
    """Local branch names for the chip's dropdown (current branch included;
    sorted by git's default ordering). Empty when not a repo / unborn HEAD."""
    root = Path(root)
    rc, out = await _run_git(root, "branch", "--format=%(refname:short)")
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def invalidate_git_caches(root: Path | str) -> None:
    """Force the next info/branch poll to re-read from git (after a UI-driven
    checkout, commit, push, pull ...)."""
    root = Path(root)
    _invalidate_branch_cache(root)
    _info_cache.pop(str(root), None)
