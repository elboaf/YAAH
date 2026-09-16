"""Git workspace info for the UI (current branch), kept out of the tool
layer on purpose: this is app state, not a model tool.

Cheap by design — stat() the .git/HEAD file and only spawn git when it
changed. The chat panel re-polls the endpoint every couple of seconds, so a
terminal `git checkout` reflects in the UI without any push channel, and a
non-repo workspace costs one failed stat per poll.
"""
import asyncio
import time
from pathlib import Path

# (resolved workspace root) -> (mtime_ns captured at last read, branch text)
_cache: dict[str, tuple[float, str | None]] = {}


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
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
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
