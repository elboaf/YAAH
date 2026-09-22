"""Resolve real git.exe on hosts where only a git.cmd shim is on PATH.

Test-environment helper (Windows sandbox images ship git as a .cmd shim
for the agent's shell, and CreateProcess cannot exec a .cmd directly).
Tries plain `git` first (normal hosts); falls back to the known toolkit
shim target. Cached; asserts clearly when git is not runnable at all.
"""

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def git_exe() -> str:
    """The git executable to use for direct subprocess spawn in tests."""
    direct = shutil.which("git")
    if direct and direct.lower().endswith(".exe"):
        return direct
    for cand in (
        Path(os.environ.get("TOOLKIT", "")) / "mingit" / "cmd" / "git.exe",
        Path.home() / "Desktop" / "toolkit" / "mingit" / "cmd" / "git.exe",
        Path("C:/Program Files/Git/cmd/git.exe"),
    ):
        if cand.is_file():
            return str(cand)
    if direct:  # a shim that CreateProcess CAN run (not our sandbox case)
        return direct
    raise RuntimeError("git.exe not found for tests")


@lru_cache(maxsize=None)
def _GIT_ENV() -> dict:
    """Environment for spawned git: set an identity so commits work on
    hosts without a global git user (repo tests must never depend on the
    host's git config)."""
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "yaah-test")
    env.setdefault("GIT_AUTHOR_EMAIL", "yaah-test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "yaah-test")
    env.setdefault("GIT_COMMITTER_EMAIL", "yaah-test@example.com")
    env.setdefault("GIT_CONFIG_GLOBAL", os.devnull)
    return env


def run_git(cwd, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [git_exe(), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_GIT_ENV(),
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stdout}{proc.stderr}")
    return proc
