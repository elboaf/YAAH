"""Detached process lifecycle (issue #770).

Every ``bash``/``powershell`` call wraps the child in a Job Object with
kill-on-job-close and tears that job down the instant the call returns, so
any deliberately backgrounded server dies with the call that started it
(the issue's root cause). The two executors here spawn a child WITHOUT that
Job Object -- ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
CREATE_NO_WINDOW`` -- so it outlives both the tool call and the backend
process, and they keep an in-process registry keyed by PID so the backend
stays the source of truth for "what the agent started" (the shape the UI
panel reads at stage 2).

Windows-only in effect: only Windows has the Job Object this replaces and
the spawn flags are Windows-only. The module imports cleanly on POSIX (the
flags resolve to 0 there), so the two tools simply never reach a model off
Windows because the schema is gated to ``os.name == "nt"``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
import threading
import time

log = logging.getLogger("yaah.detached")

# Windows creation flags. ``DETACHED_PROCESS`` and ``CREATE_NEW_PROCESS_GROUP``
# share the 0x200 bit; OR-ing them is harmless and keeps the intent explicit.
# POSIX accepts ``creationflags`` but ignores it -- the tool never reaches
# POSIX in practice because get_schemas() only offers it on Windows.
DETACHED_PROCESS = 0x00000200
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
_DETACH_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

# In-process registry keyed by PID: {command, cwd, log_path, started,
# conversation_id}. Advisory only -- liveness is always re-checked against
# the OS, because the child outlives the backend by design and a restarted
# backend starts with an empty registry.
_REGISTRY: dict[int, dict] = {}
_lock = threading.Lock()

# Default log dir under the workspace when the caller omits log_path.
_DEFAULT_LOG_DIR = ".yaah-detached"
_READINESS_POLL_INTERVAL = 0.2


def _alive(pid) -> bool:
    """Is pid still running? Best-effort, never raises.

    psutil first (an optional dependency), a tasklist CSV parse on Windows,
    then os.kill on POSIX. A stale registry row must report dead -- never
    trust the row itself as proof of life.
    """
    if not pid:
        return False
    try:
        import psutil

        return bool(psutil.Process(pid).is_running())
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - vanished or inaccessible: treat as dead
        return False

    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:  # noqa: BLE001
            return False

    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].strip() == str(pid):
            return True
    return False


def _split(command: str) -> list[str]:
    """Split the caller's command string into argv.

    posix=False (win32 mode): backslash is NOT an escape char, so Windows
    paths (``C:\\Users\\...``) split intact rather than collapsing to
    ``C:Users...``. On POSIX it still honours standard quoting; the shell
    tools remain the surface for Unix shell syntax.
    """
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    if not parts:
        raise ValueError("command must not be empty")
    return parts


def _resolve_log_path(workspace: str, log_path: str | None) -> str:
    """Absolute path, or a workspace-relative one; parent dir is created.

    An absolute caller path is honored as-is (the log must outlive the
    workspace if the user asked for it); anything else resolves inside the
    workspace. When omitted, a sanitized name under the default dir.
    """
    log_path = (log_path or "").strip()
    if log_path:
        p = log_path
        if not os.path.isabs(p):
            p = os.path.join(workspace or ".", p)
    else:
        stem = re.sub(r"\W", "_", (workspace or "").strip().replace(os.sep, "_"))[:40]
        stem = stem or "detached"
        p = os.path.join(workspace or ".", _DEFAULT_LOG_DIR, f"{stem}.log")

    parent = os.path.dirname(p) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:  # noqa: BLE001 - fall back to the caller's path as-is
        pass
    return p


def _record(pid, *, command, cwd, log_path, conversation_id) -> dict:
    entry = {
        "pid": pid,
        "command": command,
        "cwd": cwd,
        "log_path": log_path,
        "started": time.time(),
        "conversation_id": conversation_id,
    }
    with _lock:
        _REGISTRY[pid] = entry
    return entry


def _recorded(pid) -> dict | None:
    with _lock:
        return _REGISTRY.get(pid)


def _prune_dead() -> None:
    """Drop registry rows whose process is gone, so the list never accumulates
    dead entries (used by list_processes; keeps the registry honest)."""
    dead = [pid for pid in list(_REGISTRY) if not _alive(pid)]
    if dead:
        with _lock:
            for pid in dead:
                _REGISTRY.pop(pid, None)


async def _wait_for_port(port: int, timeout: float) -> bool:
    """Report ready only after 127.0.0.1:port accepts a connection.

    Uses ``asyncio.open_connection`` (which works on every platform, including
    Windows' ProactorEventLoop where ``loop.connect_ex`` does not exist) with
    an ``asyncio.wait_for`` guard so a dead port can never hang the tool past
    its timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port),
                                   timeout=min(0.5, max(0.1, timeout)))
            return True
        except (OSError, asyncio.TimeoutError):
            pass
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_READINESS_POLL_INTERVAL)


def _log_tail(path: str, lines: int = 20) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


async def spawn_detached(
    workspace: str,
    command: str = "",
    log_path: str = "",
    cwd: str = "",
    ready_port: int | None = None,
    timeout_seconds: int = 10,
    conversation_id: int | None = None,
) -> dict:
    """Spawn ``command`` detached so it outlives the tool call and the backend.

    No job object, stdin devnull, stdout/stderr to a log file, no inherited
    pipe. Returns the PID and log path; with ``ready_port`` the call blocks
    until the port accepts a connection or the timeout expires.
    """
    if not command or not str(command).strip():
        return {"error": "command is required: the command line to launch detached"}

    cwd = (cwd or "").strip() or workspace or "."
    timeout = max(0.1, min(float(timeout_seconds or 10), 120))

    pid = _find_duplicate(str(command))
    if pid is not None:
        return {
            "error": (
                f"already running: a process launched with the same command "
                f"(pid {pid}) is alive. Stop it before starting another."
            ),
            "pid": pid,
            "started": False,
        }

    log_file = _resolve_log_path(workspace, log_path)
    argv = _split(str(command))
    try:
        fh = open(log_file, "ab", buffering=0)
    except OSError as e:
        return {"error": f"cannot open log file {log_file!r}: {e}", "started": False}

    env = dict(os.environ)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=fh,
            env=env,
            creationflags=_DETACH_FLAGS,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        fh.close()
        return {"error": f"{type(e).__name__}: {e}", "started": False}
    finally:
        fh.close()

    _record(proc.pid, command=str(command), cwd=str(cwd),
            log_path=log_file, conversation_id=conversation_id)

    log.info("spawn_detached pid=%s cmd=%s log=%s", proc.pid, str(command), log_file)

    if ready_port is not None:
        ready = await _wait_for_port(int(ready_port), timeout)
        if not ready:
            return {
                "pid": proc.pid,
                "started": True,
                "log_path": log_file,
                "ready": False,
                "error": (
                    f"port {int(ready_port)} did not accept a connection within "
                    f"{timeout:g}s; tail of {log_file}:"
                ),
                "log_tail": _log_tail(log_file),
            }

    return {
        "pid": proc.pid,
        "started": True,
        "log_path": log_file,
        "cwd": str(cwd),
        "ready": True,
    }


async def stop_process(workspace, pid, conversation_id=None) -> dict:
    """Kill a process the backend recorded as agent-spawned (tree, not root).

    Refuses any PID absent from the registry: stopping by PID kills an
    arbitrary process, so the registry is the gate.
    """
    entry = _recorded(pid)
    if entry is None:
        return {"stopped": False, "error": f"no agent-spawned process with pid {pid}"}

    killed = False
    if os.name == "nt":
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                capture_output=True, text=True, timeout=15,
            )
            killed = res.returncode == 0
        except Exception:  # noqa: BLE001
            killed = False
    else:
        try:
            os.killpg(os.getpgid(int(pid)), 9)
            killed = True
        except Exception:  # noqa: BLE001
            killed = False

    with _lock:
        _REGISTRY.pop(pid, None)

    return {
        "stopped": True,
        "pid": int(pid),
        "command": entry.get("command"),
        "killed": killed,
    }


def list_processes() -> list[dict]:
    """Registry entries with live-liveness applied (for the stage-2 panel).

    A row is reported alive only if the OS still shows the PID; dead rows are
    pruned so the list does not accumulate stale entries.
    """
    _prune_dead()
    rows = []
    with _lock:
        for pid, entry in _REGISTRY.items():
            rows.append({**entry, "alive": _alive(pid)})
    return rows


def _find_duplicate(command: str):
    """PID of an already-running registry entry launched with the same command
    string, or None. Used to refuse stacking a duplicate on a live one."""
    with _lock:
        matches = [e for e in _REGISTRY.values() if e["command"] == command]
    for entry in matches:
        if _alive(entry["pid"]):
            return entry["pid"]
    return None


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "spawn_detached",
            "description": (
                "Start a long-running process (a dev server, a watcher) that "
                "must survive after this tool call returns. Unlike bash/"
                "powershell, this spawns the child WITHOUT the kill-on-close "
                "Job Object, so it outlives the call and the backend. Takes a "
                "single command line (quoted as needed), a log path for "
                "stdout/stderr (omit to default it under the workspace), an "
                "optional working dir, and an optional ready_port to wait on "
                "(the call blocks until that TCP port accepts a connection, "
                "replacing a sleep-then-check). Returns the PID and log path; "
                "read the log with read_file. Use stop_process to end it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command line to launch (quoted as needed), e.g. 'node server.js'.",
                    },
                    "log_path": {
                        "type": "string",
                        "description": "Absolute path or workspace-relative path for stdout/stderr. Omit to default it under the workspace.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the child (defaults to the workspace).",
                    },
                    "ready_port": {
                        "type": "integer",
                        "description": "Optional TCP port; the call blocks until it accepts a connection or the timeout expires.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max seconds to wait for ready_port (default 10, max 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_process",
            "description": (
                "Stop a process this backend started with spawn_detached, "
                "killing its whole tree. Only PIDs the backend recorded can be "
                "stopped; any other PID is refused. Use the pid returned by "
                "spawn_detached."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {
                        "type": "integer",
                        "description": "The pid returned by spawn_detached.",
                    },
                },
                "required": ["pid"],
            },
        },
    },
]

EXECUTORS = {
    "spawn_detached": spawn_detached,
    "stop_process": stop_process,
}
