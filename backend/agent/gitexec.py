"""Remote git gateway: the one seam where every git read resolves to an
executor (issue #333, spec #332; ADR-0010 remote-parity amendment).

Local workspaces keep today's local git execution (the gitinfo/_run_git
subprocess path). Remote workspaces ship git through the existing remote
exec channel — the same bash tool the agent already uses on the host —
so the host learns nothing new (zero host-side code, protocol untouched;
the ADR-0010 amendment's rejected alternative: a structured git-ops
protocol).

Distinguishability contract — the reason this module exists:

    (rc, output)  git ran (the answer may be "not a repo": that is DATA)
    None          git could not run at all (host offline, channel error,
                  malformed reply) — callers surface an explicit offline
                  state instead of reading unreachability as absence

Commands are composed as a bare arg list joined with single spaces and
must stay cross-dialect safe: on a Windows host without Git Bash the
remote bash tool runs cmd.exe, so the corpus never relies on quoting,
parens, shell chaining, or globbing. cwd is already the workspace on the
host side, so no -C <path> and no path quoting are ever needed. One git
operation = one channel round-trip (accepted cost, spec #332); gitinfo
caches the composed readout per its usual TTL, so pollers share one hop.
"""

from __future__ import annotations

import asyncio
import os

from backend.agent.remote import parse_ns, remote_for_workspace

# Windows: suppress the console window a console child of the windowed app
# would pop up. POSIX: own process group (mirrors gitinfo/tools).
_SUBPROCESS_FLAGS = (
    {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
)

# The channel rides the host's bash tool (max 300s there); this layer is a
# read-only UI poll — git answers in milliseconds or the host is dying.
_REMOTE_GIT_TIMEOUT = 30.0

# Characters whose shell reading differs between POSIX sh and cmd.exe
# (quoting, expansion, chaining, redirection, history, variable syntax).
# The composed corpus stays clear of all of them; test_issue_333 guards
# the same rule from the outside.
_UNSAFE_CHARS = set("\"'`;&|()<>*?[]{}~$\\^%!#,")


async def run_git(
    workspace: str | os.PathLike,
    *args: str,
) -> tuple[int, str] | None:
    """One git invocation in `workspace`, local or remote; (rc, output) or
    None when no executor could run git at all. Never raises."""
    ws = str(workspace)
    if parse_ns(ws) is None:
        return await _run_git_local(ws, *args)
    return await _run_git_remote(ws, *args)


async def _run_git_local(
    root: str,
    *args: str,
    merge_stderr: bool = True,
) -> tuple[int, str]:
    """The local executor: the subprocess shape gitinfo.py has always had
    (--no-optional-locks so a read-only poll never contends with the
    agent's own git writes, issue #279; merged stderr by default; the
    127/124 rc conventions). merge_stderr=False discards stderr — the
    branch-lookup ambiguity guard (a local branch named HEAD)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "--no-optional-locks", "-C", root, *args,
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
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "git timed out"
    return proc.returncode, out.decode("utf-8", errors="replace").strip()


def _cross_dialect_command(*args: str) -> str | None:
    """The remote corpus: bare args joined with single spaces. None when an
    arg would force shell-dependent reading — the command must parse
    identically in POSIX sh and cmd.exe (no quoting, chaining, globs,
    expansions, path separators of either flavor)."""
    for arg in args:
        if not arg:
            return None
        if any(ch in _UNSAFE_CHARS for ch in arg):
            return None
    return "git " + " ".join(args)


async def _run_git_remote(
    workspace: str, *args: str
) -> tuple[int, str] | None:
    """Ship one git command through the remote exec channel. The channel's
    bash tool already executes at the workspace root (the host strips the
    namespace and runs there), so the composition is cwd-free by design.
    None = git could not run (host unknown/offline, channel error,
    malformed reply, uncomposable command, timeout)."""
    command = _cross_dialect_command(*args)
    if command is None:
        return None
    session = remote_for_workspace(workspace)
    if session is None:
        return None
    try:
        result = await asyncio.wait_for(
            session.exec_tool("bash", {"command": command}, workspace=workspace),
            timeout=_REMOTE_GIT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return None
    if not isinstance(result, dict):
        return None
    if "error" in result:
        return None
    if result.get("timed_out"):
        return None
    exit_code = result.get("exit_code")
    output = result.get("output")
    if not isinstance(exit_code, int) or not isinstance(output, str):
        return None
    return exit_code, output.strip()
