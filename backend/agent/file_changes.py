"""Per-run workspace change snapshots for the transcript summary."""

from __future__ import annotations

import asyncio
import difflib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorkspaceSnapshot:
    """Workspace file contents captured at one point in the run."""

    workspace: str
    files: dict[str, bytes]


async def _run_git(workspace: str, *args: str) -> tuple[int, bytes]:
    # Use the same portable Git discovery as worktree isolation on Windows.
    from backend.agent.worktrees import _git_exe

    try:
        proc = await asyncio.create_subprocess_exec(
            _git_exe(),
            *args,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **(
                {"creationflags": 0x08000000}
                if os.name == "nt"
                else {"start_new_session": True}
            ),
        )
    except OSError:
        return 127, b""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b""
    return proc.returncode or 0, out


def _read_path(path: Path) -> bytes | None:
    try:
        if path.is_symlink():
            return os.fsencode(os.readlink(path))
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    return None


def _filesystem_snapshot(workspace: str) -> dict[str, bytes]:
    """Capture a non-Git folder without following symlinks or entering .git."""
    root = Path(workspace)
    files: dict[str, bytes] = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name != ".git"]
        base = Path(current)
        for name in names:
            path = base / name
            content = _read_path(path)
            if content is None:
                continue
            files[path.relative_to(root).as_posix()] = content
    return files


async def _git_files_snapshot(workspace: str, repo_root: Path) -> dict[str, bytes]:
    """Capture tracked plus non-ignored untracked files, scoped to workspace."""
    rc, output = await _run_git(
        workspace,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--full-name",
        "-z",
        "--",
        ".",
    )
    if rc != 0:
        return _filesystem_snapshot(workspace)

    root = Path(workspace)
    files: dict[str, bytes] = {}
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        repo_path = repo_root / Path(os.fsdecode(raw_path))
        try:
            relative = repo_path.relative_to(root)
        except ValueError:
            continue
        content = _read_path(repo_path)
        if content is not None:
            files[relative.as_posix()] = content
    return files


async def snapshot_workspace(workspace: str) -> WorkspaceSnapshot:
    """Capture tracked and non-ignored files without modifying Git's index."""
    workspace = str(Path(workspace).resolve())
    rc, root_bytes = await _run_git(workspace, "rev-parse", "--show-toplevel")
    if rc == 0:
        repo_root = Path(os.fsdecode(root_bytes).strip())
        files = await _git_files_snapshot(workspace, repo_root)
    else:
        files = _filesystem_snapshot(workspace)
    return WorkspaceSnapshot(workspace=workspace, files=files)


def _text_line_counts(before: bytes, after: bytes) -> tuple[int, int, bool]:
    if b"\0" in before or b"\0" in after:
        return 0, 0, True
    try:
        old_lines = before.decode("utf-8").splitlines()
        new_lines = after.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return 0, 0, True
    added = deleted = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=old_lines, b=new_lines, autojunk=False
    ).get_opcodes():
        if tag in ("replace", "delete"):
            deleted += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, deleted, False


def _filesystem_diff(before: dict[str, bytes], after: dict[str, bytes]) -> list[dict]:
    rows = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path, b""), after.get(path, b"")
        if old == new and path in before and path in after:
            continue
        added, deleted, binary = _text_line_counts(old, new)
        rows.append({"path": path, "added": added, "deleted": deleted, "binary": binary})
    return rows


async def diff_snapshots(
    before: WorkspaceSnapshot, after: WorkspaceSnapshot
) -> list[dict]:
    """Return per-file net line changes between two run snapshots."""
    return _filesystem_diff(before.files, after.files)


def summarize_file_changes(files: list[dict]) -> dict | None:
    """Add aggregate line counts and omit empty/no-op summaries."""
    if not files:
        return None
    return {
        "files": files,
        "added": sum(file.get("added", 0) for file in files),
        "deleted": sum(file.get("deleted", 0) for file in files),
    }
