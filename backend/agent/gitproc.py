"""Git process plumbing: executable discovery + spawn conventions.

Extracted from worktrees.py (ADR 0007 ticket 2: tenant split). This is
platform plumbing, not worktree lifecycle: one module answers "how does
this codebase locate and spawn git", so callers stop importing a
private (`_git_exe`) from a module whose real subject is sessions, and
worktrees no longer borrows subprocess flags back from tools.py.

Everything here is dependency-light on purpose: only stdlib, so any
agent module (file_changes, git_activity, main's UI git path) can use
it without import cycles.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Windows: suppress the console window a console child of the windowed
# app would pop up. POSIX subprocess has no creationflags parameter.
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}
# POSIX: run children in their own process group so a timeout can kill
# the whole tree. Windows uses a kill-on-terminate Job Object instead.
NEW_SESSION = {} if os.name == "nt" else {"start_new_session": True}

_GIT_EXE: str | None = None


def git_exe() -> str:
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
