"""Tool registry: schema definitions + safe execution.

Tools are the agent's hands. Each tool has an OpenAI-format JSON schema for
the model and an async executor. All paths are resolved against a workspace
root and validated to prevent escapes.
"""
import asyncio
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path


# ---------------------------------------------------------------- path safety

# Windows: suppress the console window a console child of the windowed app
# would pop up. POSIX subprocess has no creationflags parameter.
_NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}
# POSIX: run children in their own process group so a timeout can kill the
# whole tree. Windows uses a kill-on-terminate Job Object instead (taskkill
# /T fails when the shell root has already exited, e.g. after `start /b`).
_NEW_SESSION = {} if os.name == "nt" else {"start_new_session": True}

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOB_BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOB_EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOB_BASIC_LIMITS),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def _job_create():
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPWSTR, wintypes.LPWSTR]
        job = kernel32.CreateJobObjectW(None, None)
        info = _JOB_EXTENDED_LIMITS()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info))
        return job

    def _job_assign(job, pid):
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            kernel32.AssignProcessToJobObject(job, handle)
        finally:
            kernel32.CloseHandle(handle)

    def _job_kill(job):
        ctypes.windll.kernel32.TerminateJobObject(job, 1)

    def _job_close(job):
        ctypes.windll.kernel32.CloseHandle(job)
else:
    _job_create = _job_assign = _job_kill = _job_close = (lambda *a: None)


def workspace_root(workspace: str | None) -> Path:
    """Resolve the workspace root directory.

    The Default pseudo-workspace (empty or legacy '.') has no stored root;
    it points at the user's home directory so file tools and the file tree
    work out of the box.
    """
    ws = (workspace or "").strip()
    if not ws or ws == ".":
        return Path.home().resolve()
    return Path(ws).resolve()


def resolve_path(workspace: str, rel_path: str, for_write: bool = False) -> Path:
    """Resolve rel_path inside workspace; raise ValueError on escape.

    Read-only exception: paths under the skills root (~/.yaah/skills) are
    allowed, so multi-file skills can have the model read their own
    supporting files. Writes there stay blocked — skills are user-authored.
    """
    root = workspace_root(workspace)
    p = (root / rel_path).resolve()
    if p == root or root in p.parents:
        return p
    if not for_write:
        skills_root = Path(
            os.environ.get("YAAH_SKILLS_PATH") or Path.home() / ".yaah" / "skills"
        ).resolve()
        if p == skills_root or skills_root in p.parents:
            return p
    raise ValueError(f"Path escapes workspace: {rel_path}")


# ---------------------------------------------------------------- schemas

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command in the workspace directory. Use for "
                "builds, tests, git, file discovery (ls, grep, find), and text "
                "processing. Long-running commands will time out. Never shell-"
                "background a long-running process (trailing &, start /b): the "
                "child outlives the tool call, keeps the output pipe open, and "
                "wedges the session. Servers and watchers need a detached spawn "
                "instead (e.g. Start-Process with redirect, or nohup with "
                "stdout/stderr redirected to a file)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 60, max 300)",
                    },
                },
                "required": ["command"],
            },
        },
    },
]

# Windows-only: not in TOOLS_SCHEMA (which must stay platform-neutral);
# get_schemas() appends it when running on Windows so non-Windows models
# never see a tool they can't use.
POWERSHELL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "powershell",
        "description": (
            "Execute a command in Windows PowerShell from the workspace "
            "directory. Use for Windows-native tasks the shell can't do "
            "well: registry, services, WMI/CIM, ACLs, scheduled tasks, "
            "structured object pipelines. Long-running commands will "
            "time out. Never shell-background a long-running process "
            "(trailing &): the child outlives the tool call, keeps the "
            "output pipe open, and wedges the session. Servers and watchers "
            "need Start-Process (optionally -WindowStyle Hidden) instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The PowerShell command to run"},
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 60, max 300)",
                },
            },
            "required": ["command"],
        },
    },
}

TOOLS_SCHEMA += [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web with DuckDuckGo (free, no API key). Returns "
                "a numbered list of title / URL / snippet. Use web_fetch to "
                "read a result in full."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "description": "Max results (default 8)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a URL and return the page's readable text (HTML "
                "stripped to plain text), via a real headless browser so "
                "JS-heavy and bot-guarded sites usually work. Use after "
                "web_search to read a result, or directly for a known URL. "
                "If blocked, fall back to web_search snippets rather than "
                "retrying the same URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "Max text chars (default 20000)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": (
                "Download an image from a URL and attach it so you can see "
                "it (requires a vision-capable model). Use after "
                "web_search or web_fetch — web_fetch lists the page's "
                "image URLs under 'IMAGES ON PAGE'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Put a decision to the user and wait for their answer. One "
                "question per call. Provide 2-4 concrete answer options, "
                "each with a short description of what the choice means. "
                "The user can pick an option or answer with free text. Use "
                "this for decisions, preferences, and ambiguities only — "
                "never for facts you can look up yourself with tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to put to the user",
                    },
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Short answer text",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "What this choice means / its trade-offs",
                                },
                            },
                            "required": ["label"],
                        },
                        "description": "2-4 concrete answer options",
                    },
                },
                "required": ["question", "options"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file in the workspace. Large files are "
                "returned in line-range chunks: use start_line/end_line to page "
                "through, and the truncated/total_lines fields to navigate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "1-based first line to read"},
                    "end_line": {"type": "integer", "description": "1-based last line to read"},
                    "offset_line": {"type": "integer", "description": "Alias for start_line"},
                    "limit_lines": {"type": "integer", "description": "Max lines to return"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or completely overwrite an existing one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact, unique occurrence of old_text with new_text "
                "in an existing file. Prefer this over write_file for edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Create a new file with content. Fails if the file already "
                "exists; use write_file to overwrite."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file (or empty directory) inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file/directory within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Source path (relative)"},
                    "dst": {"type": "string", "description": "Destination path (relative)"},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search the workspace. Provide pattern to regex-search file "
                "CONTENTS (with optional glob filter on filenames), or glob "
                "alone to match file PATHS. Returns matches grouped by file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex to match against file contents"},
                    "glob": {"type": "string", "description": "Filename glob filter, e.g. '*.py'"},
                    "max_results": {"type": "integer", "description": "Max matching lines (default 100)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show git working tree status for the workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show git diff. Set staged=true for staged changes, or pass a path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean", "description": "Diff staged changes only"},
                    "path": {"type": "string", "description": "Limit diff to this path"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_add",
            "description": "Stage files in git. Omit paths to stage everything.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Commit staged changes with a message.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_push",
            "description": "Push commits to the remote.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_pull",
            "description": "Pull and integrate changes from the remote.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load a skill's full instructions into this conversation. "
                "Use when the user's task matches an available skill's "
                "description (see the skills list in your system prompt). "
                "The skill's instructions are added to your system prompt "
                "for the rest of this turn; follow them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill's name, exactly as listed",
                    },
                },
                "required": ["name"],
            },
        },
    },
]


# ---------------------------------------------------------------- executors

MAX_BASH_TIMEOUT = 300
MAX_OUTPUT_CHARS = 20_000


def _kill_tree(proc: asyncio.subprocess.Process, job=None) -> None:
    """Kill a timed-out process and its children. Children matter: a
    backgrounded server (``cmd &``) inherits the output pipe, so killing
    only the shell leaks an orphan that wedges later calls. On Windows the
    job handle (assigned at spawn) is the only reliable way — the shell
    root may already be dead, so taskkill /T finds no tree."""
    try:
        if job:
            _job_kill(job)
        elif os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, **_NO_WINDOW,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 — process may already be gone
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Wait out a killed process. The prior communicate() was cancelled
    mid-read, so a second communicate() is unsupported and can hang even
    after the tree is dead; wait() just needs the exit."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass


async def run_bash(workspace: str, command: str, timeout_seconds: int = 60) -> dict:
    """Run a shell command in the workspace; return structured result."""
    timeout = max(1, min(int(timeout_seconds or 60), MAX_BASH_TIMEOUT))
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace_root(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_NO_WINDOW, **_NEW_SESSION,
        )
        job = _job_create()
        if job:
            _job_assign(job, proc.pid)
        try:
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                output = out.decode("utf-8", errors="replace")
                timed_out = False
            except asyncio.TimeoutError:
                _kill_tree(proc, job)
                # communicate() was cancelled mid-read; calling it again is
                # unsupported and can hang forever. wait() only needs the exit.
                await _reap(proc)
                output = f"[timed out after {timeout}s]"
                timed_out = True
        finally:
            _job_close(job)

        truncated = False
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS]
            truncated = True

        return {
            "exit_code": proc.returncode,
            "output": output,
            "timed_out": timed_out,
            "truncated": truncated,
        }
    except Exception as e:  # noqa: BLE001
        return {"exit_code": -1, "output": f"error: {e}", "timed_out": False, "truncated": False}


async def run_powershell(workspace: str, command: str, timeout_seconds: int = 60) -> dict:
    """Run a Windows PowerShell command in the workspace; same structured
    result shape as run_bash."""
    timeout = max(1, min(int(timeout_seconds or 60), MAX_BASH_TIMEOUT))
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", command,
            cwd=workspace_root(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_NO_WINDOW, **_NEW_SESSION,
        )
        job = _job_create()
        if job:
            _job_assign(job, proc.pid)
        try:
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                output = out.decode("utf-8", errors="replace")
                timed_out = False
            except asyncio.TimeoutError:
                _kill_tree(proc, job)
                await _reap(proc)
                output = f"[timed out after {timeout}s]"
                timed_out = True
        finally:
            _job_close(job)

        truncated = False
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS]
            truncated = True

        return {
            "exit_code": proc.returncode,
            "output": output,
            "timed_out": timed_out,
            "truncated": truncated,
        }
    except Exception as e:  # noqa: BLE001
        return {"exit_code": -1, "output": f"error: {e}", "timed_out": False, "truncated": False}


async def read_file(
    workspace: str,
    path: str,
    start_line: int = None,
    end_line: int = None,
    offset_line: int = None,
    limit_lines: int = None,
) -> dict:
    """Read a file, optionally returning a 1-based line window."""
    p = resolve_path(workspace, path)
    if not p.exists():
        return {"error": f"File not found: {path}"}
    if p.is_dir():
        return {"error": f"Is a directory: {path}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)

        start = (start_line or offset_line)
        start = (start - 1) if start and start > 0 else 0
        if start >= total:
            return {"path": path, "total_lines": total, "lines": [], "content": ""}
        if end_line and end_line >= start + 1:
            end = min(end_line, total)
        elif limit_lines and limit_lines > 0:
            end = start + limit_lines
        else:
            end = total
        window = lines[start:end]

        numbered = "\n".join(f"{start + i + 1:6d}\t{line}" for i, line in enumerate(window))
        return {
            "path": path,
            "total_lines": total,
            "start_line": start + 1,
            "end_line": end,
            "content": numbered,
            "truncated": end < total,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def write_file(workspace: str, path: str, content: str) -> dict:
    """Write content to a file (create parent dirs); full overwrite."""
    p = resolve_path(workspace, path, for_write=True)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {
            "path": path,
            "bytes_written": p.stat().st_size,
            "lines_written": len(content.splitlines()),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def edit_file(workspace: str, path: str, old_text: str, new_text: str) -> dict:
    """Exact-match single replacement; fails loudly on ambiguity or no match."""
    p = resolve_path(workspace, path, for_write=True)
    if not p.exists():
        return {"error": f"File not found: {path}"}
    try:
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count == 0:
            return {"error": "old_text not found in file", "path": path}
        if count > 1:
            return {"error": f"old_text matches {count} locations; must be unique", "path": path}
        p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return {"path": path, "replaced": 1}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ---------------------------------------------------------------- new file tools

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
    "build", ".pytest_cache", ".mypy_cache", "target", ".next",
}


async def create_file(workspace: str, path: str, content: str) -> dict:
    """Create a new file; refuses to clobber an existing one."""
    p = resolve_path(workspace, path, for_write=True)
    if p.exists():
        return {"error": f"File already exists: {path}. Use write_file to overwrite."}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": path, "bytes_written": p.stat().st_size}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def delete_file(workspace: str, path: str) -> dict:
    """Delete a file or an empty directory inside the workspace."""
    p = resolve_path(workspace, path, for_write=True)
    if p == workspace_root(workspace):
        return {"error": "Refusing to delete the workspace root"}
    if not p.exists():
        return {"error": f"Not found: {path}"}
    try:
        if p.is_dir():
            p.rmdir()  # only empty dirs; non-empty needs explicit bash rm -rf
            return {"path": path, "deleted": "directory (empty)"}
        p.unlink()
        return {"path": path, "deleted": True}
    except OSError as e:
        return {"error": str(e)}


async def move_file(workspace: str, src: str, dst: str) -> dict:
    """Move/rename within the workspace."""
    s = resolve_path(workspace, src, for_write=True)
    d = resolve_path(workspace, dst, for_write=True)
    if not s.exists():
        return {"error": f"Source not found: {src}"}
    if d.exists():
        return {"error": f"Destination already exists: {dst}"}
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        s.rename(d)
        return {"src": src, "dst": dst, "moved": True}
    except OSError as e:
        return {"error": str(e)}


async def search_files(
    workspace: str,
    pattern: str = None,
    glob: str = None,
    max_results: int = 100,
) -> dict:
    """Regex-search file contents (optionally glob-filtered) or match paths."""
    root = workspace_root(workspace)
    max_results = max(1, min(int(max_results or 100), 500))
    content_re = None
    if pattern:
        try:
            content_re = re.compile(pattern)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

    import fnmatch

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for f in filenames:
            files.append(Path(dirpath) / f)

    matches: list[dict] = []
    truncated = False
    for f in sorted(files):
        rel = f.relative_to(root).as_posix()
        if glob and not fnmatch.fnmatch(f.name, glob) and not fnmatch.fnmatch(rel, glob):
            continue
        if content_re is None:
            if len(matches) >= max_results:
                truncated = True
                break
            matches.append({"path": rel})
            continue
        try:
            if f.stat().st_size > 1_000_000:
                continue  # skip huge files
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if content_re.search(line):
                if len(matches) >= max_results:
                    truncated = True
                    break
                matches.append({"path": rel, "line": lineno, "text": line.strip()[:200]})
        if truncated:
            break

    return {"matches": matches, "count": len(matches), "truncated": truncated}


# ---------------------------------------------------------------- git tools

async def _git(workspace: str, *args: str) -> dict:
    """Run a git command in the workspace; return structured result."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=workspace_root(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **_NO_WINDOW, **_NEW_SESSION,
    )
    job = _job_create()
    if job:
        _job_assign(job, proc.pid)
    try:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            _kill_tree(proc, job)
            await _reap(proc)
            return {"error": "git timed out"}
    finally:
        _job_close(job)
    output = out.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        return {"error": output.strip()[:2000], "exit_code": proc.returncode}
    return {"output": output.strip()[:MAX_OUTPUT_CHARS], "exit_code": 0}


async def git_status(workspace: str) -> dict:
    return await _git(workspace, "status", "--short", "--branch")


async def git_diff(workspace: str, staged: bool = False, path: str = None) -> dict:
    args = ["diff"]
    if staged:
        args.append("--staged")
    if path:
        args += ["--", path]
    return await _git(workspace, *args)


async def git_add(workspace: str, paths: list = None) -> dict:
    args = ["add", "-A"] if not paths else ["add", *paths]
    return await _git(workspace, *args)


async def git_commit(workspace: str, message: str) -> dict:
    return await _git(workspace, "commit", "-m", message)


async def git_push(workspace: str) -> dict:
    return await _git(workspace, "push")


async def git_pull(workspace: str) -> dict:
    return await _git(workspace, "pull")


# ---------------------------------------------------------------- dispatch

from backend.agent.webtools import view_image, web_fetch, web_search

EXECUTORS = {
    "bash": run_bash,
    "powershell": run_powershell,
    "web_search": web_search,
    "web_fetch": web_fetch,
    "view_image": view_image,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "create_file": create_file,
    "delete_file": delete_file,
    "move_file": move_file,
    "search_files": search_files,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_add": git_add,
    "git_commit": git_commit,
    "git_push": git_push,
    "git_pull": git_pull,
}

SCHEMAS = {s["function"]["name"]: s for s in TOOLS_SCHEMA}


async def execute_tool(name: str, arguments: dict, workspace: str) -> dict:
    """Execute a tool by name with a dict of arguments. Never raises."""
    fn = EXECUTORS.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}. Available: {sorted(EXECUTORS)}"}
    try:
        return await fn(workspace=workspace, **arguments)
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def get_schemas() -> list:
    # powershell only exists on Windows (powershell.exe); exposing it
    # elsewhere just invites the model to attempt Windows commands.
    if os.name == "nt":
        return TOOLS_SCHEMA + [POWERSHELL_SCHEMA]
    return TOOLS_SCHEMA
