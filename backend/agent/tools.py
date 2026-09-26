"""Tool registry: schema definitions + safe execution.

Tools are the agent's hands. Each tool has an OpenAI-format JSON schema for
the model and an async executor. All paths are resolved against a workspace
root and validated to prevent escapes.
"""
import asyncio
import codecs
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path

from backend.agent.ghenv import command_env
from backend.agent.shell import resolve_git_bash


# ---------------------------------------------------------------- path safety


def _tool_env() -> dict:
    """Environment inherited by agent shell tools, including bundled gh."""
    from backend.agent.ghenv import command_env

    return command_env()

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
                "stdout/stderr redirected to a file). For a test suite longer "
                "than the timeout cap, run it in chunks (per directory or "
                "file) instead of one monolithic run. git must never open its "
                "editor: pass -m '<message>' to git commit and use "
                "GIT_EDITOR=true for git rebase --continue / tag -a / "
                "commit --amend - an interactive editor blocks the tool "
                "until it times out. For GitHub operations (PRs, issues, "
                "releases, CI checks), prefer the gh CLI over web_fetch "
                "scraping - check availability with 'gh --version' before "
                "assuming it's missing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 60, max 900)",
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
            "need Start-Process (optionally -WindowStyle Hidden) instead. "
            "git must never open its editor: pass -m '<message>' to git "
            "commit and use GIT_EDITOR=true for git rebase --continue / "
            "commit --amend - an interactive editor blocks the tool "
            "until it times out. For GitHub operations (PRs, issues, "
            "releases, CI checks), prefer the gh CLI over web_fetch "
            "scraping - check availability with 'gh --version' before "
            "assuming it's missing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                    "command": {"type": "string", "description": "The PowerShell command to run"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 60, max 900)",
                    },
            },
            "required": ["command"],
        },
    },
}

# Windows-only, agent-offered: appended by get_schemas() only when git is
# missing AND the bundled installer shipped (backend/agent/gitenv.py).
INSTALL_GIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "install_git",
        "description": (
            "Install Git for Windows silently from the installer bundled "
            "with YAAH (~minute-long setup, no windows). Offer this to "
            "the user via ask_user when a git command or tool fails "
            "because git is missing, and run it only after they agree. "
            "Shells opened before the install need a restart to see git."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

# Extended, lazy-loaded documentation. The schemas above stay short; what
# lives here only reaches the model when it calls get_help("tool_name").
HELP_DOCS: dict = {
    "bash": (
        "On Windows, runs through Git Bash when installed (the Git for "
        "Windows wrapper, with POSIX shell syntax); otherwise uses the "
        "system shell, normally cmd.exe. On Linux/macOS, uses the system "
        "shell. Commands are not retried in another shell after failure. "
        "The result reports the real exit code and combined stdout/stderr; "
        "output is truncated at a cap, so tail or filter large output "
        "in the command itself. On timeout the whole process tree is "
        "killed - partial output is still returned."
    ),
    "powershell": (
        "Prefer PowerShell for structured Windows data: Get-ChildItem, "
        "Get-Process, Get-Service, registry via Get-ItemProperty, "
        "scheduled tasks via Get-ScheduledTask. Objects pipeline, so "
        "filter with Where-Object / Select-Object instead of parsing "
        "text. The result reports the real exit code; output is "
        "truncated at a cap, so filter in the command itself."
    ),
    "web_search": (
        "DuckDuckGo HTML endpoint - no API key, no JS. Snippets are "
        "short; treat them as pointers, not answers. For a specific "
        "site, add site:example.com to the query. Rate-limited: on a "
        "429 or empty result page, wait a few seconds rather than "
        "hammering retries."
    ),
    "web_fetch": (
        "Renders the page in a real headless browser, so JS-heavy and "
        "bot-guarded sites usually work, but it is slow (~seconds) - "
        "do not use it for plain static files (use bash/powershell "
        "curl or read_file for local paths). Returns readable text "
        "plus an 'IMAGES ON PAGE' list for view_image. Blocked pages: "
        "fall back to web_search snippets instead of retrying the "
        "same URL. max_chars truncates from the top - fetch a "
        "specific anchor or raise the cap for long pages."
    ),
    "view_image": (
        "Attaches the image to the conversation so a vision-capable "
        "model can see it on the NEXT turn - the current turn's "
        "reasoning does not include it. Local paths resolve relative "
        "to the workspace root. Use it for screenshots, charts, "
        "renders and UI captures; not for binary formats the model "
        "cannot render."
    ),
    "ask_user": (
        "Blocks the turn until the user answers - batch open questions "
        "into one call when they're related, but keep one QUESTION per "
        "call. Options are clickable; the user may also type free "
        "text. Never use it to ask for facts you can look up with "
        "tools (file contents, command output, web pages)."
    ),
    "read_file": (
        "Returns JSON with content, truncated and total_lines. When "
        "truncated is true, page with start_line/end_line rather than "
        "assuming the file is short. Binary files come back "
        "replacement-char mangled - use view_image for images."
    ),
    "write_file": (
        "Overwrites the entire file - read it first if you need to "
        "preserve content you haven't seen. Creates parent "
        "directories. Paths resolve inside the workspace; escapes are "
        "rejected."
    ),
    "edit_file": (
        "Replaces the FIRST exact occurrence of old_text; it must be "
        "unique in the file or the call errors with a match count. "
        "Copy old_text verbatim from read_file output - whitespace "
        "and indentation must match exactly. For multiple edits to "
        "one file, chain several edit_file calls."
    ),
    "create_file": (
        "Fails if the file already exists (use write_file to "
        "overwrite). Creates parent directories. Paths resolve inside "
        "the workspace; escapes are rejected."
    ),
    "delete_file": (
        "Deletes a file or an EMPTY directory; refuses the workspace "
        "root. There is no undo - prefer move_file to a trash name "
        "when unsure."
    ),
    "move_file": (
        "Renames or moves within the workspace; fails if the "
        "destination exists. Creates parent directories of the "
        "destination."
    ),
    "search_files": (
        "Regex-searches file CONTENTS (Python re syntax, not grep); "
        "add a glob to filter by filename. Returns matches grouped by "
        "file, capped by max_results - narrow the pattern or glob "
        "when the cap is hit rather than assuming nothing else "
        "matches."
    ),
    "git_status": (
        "Short wrapper over `git status` in the workspace; read-only."
    ),
    "git_diff": (
        "Wraps `git diff`; pass path to limit scope, staged=true for "
        "the index. Read-only."
    ),
    "git_add": (
        "Stages paths (omit to stage everything). Does not commit."
    ),
    "git_commit": (
        "Commits the staged index with -m; never opens an editor. "
        "Empty staged set errors - check git_status first."
    ),
    "git_push": (
        "Pushes the current branch; publishes with --set-upstream "
        "when none exists. Never force-pushes."
    ),
    "git_pull": (
        "Fetches and integrates remote changes for the current branch."
    ),
    "get_help": (
        "With no argument, lists every tool with its full one-line "
        "description. With a name, returns the tool's parameter "
        "schema plus extended usage notes (caveats, examples, "
        "failure modes) that are NOT in the short schema. Cheap and "
        "read-classified: call it before first use of an unfamiliar "
        "tool, or immediately after any tool errors."
    ),
    "spawn_agent": (
        "Sub-agents see ONLY the prompt you pass - include file "
        "paths, error messages, and every decision they need; they "
        "cannot ask the user questions. Launch several in one turn "
        "for parallel independent work (max 4). Announce each "
        "delegation to the user in one line. Do not delegate work "
        "that needs this conversation's context or a user decision "
        "mid-task."
    ),
}

GET_HELP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_help",
        "description": (
            "Full documentation for a tool: usage notes, caveats and "
            "examples beyond the short schema description. Call with no "
            "argument to list every available tool with a one-line "
            "summary. Use it before first use of an unfamiliar tool or "
            "when a call errored unexpectedly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Tool to document; omit to list all tools",
                },
            },
        },
    },
}


async def get_help(workspace: str = "", tool_name: str = "") -> dict:
    name = (tool_name or "").strip()
    schemas = {s["function"]["name"]: s for s in get_schemas()}
    if not name:
        lines = [
            f"- {n}: {s['function'].get('description', '').splitlines()[0]}"
            for n, s in sorted(schemas.items())
        ]
        return {
            "tools": "\n".join(lines),
            "note": (
                "Call get_help(tool_name) for a tool's full parameter "
                "schema and extended usage notes."
            ),
        }
    schema = schemas.get(name)
    if schema is None:
        close = [n for n in schemas if name.lower() in n.lower()]
        hint = f" Similar: {close}" if close else ""
        return {"error": f"Unknown tool: {name}.{hint}"}
    doc = {"name": name, "schema": schema["function"]["parameters"]}
    notes = HELP_DOCS.get(name)
    if notes:
        doc["notes"] = notes
    return doc


TOOLS_SCHEMA += [GET_HELP_SCHEMA]

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
                "image URLs under 'IMAGES ON PAGE'. Also accepts LOCAL "
                "images: a host file path, a file:/// URL, or a path "
                "relative to the workspace root - e.g. a VM screenshot "
                "written to the toolkit mount by sandbox_run "
                "(~/.yaah/toolkit/vm-screen.png) or any chart/render "
                "produced in the workspace."
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
            "name": "search_conversation_history",
            "description": (
                "Search all persisted text in this conversation, including "
                "messages before and after context compaction, tool activity, "
                "sub-agent transcripts, and the prompt summary. Provide a "
                "case-insensitive plain-text query; results contain short excerpts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Plain-text query to find in conversation history"},
                    "max_results": {"type": "integer", "description": "Maximum matches (default 10, max 50)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "Show git working tree status for the workspace. If this "
                "or any git tool fails because git is missing (Windows), "
                "offer install_git to the user via ask_user."
            ),
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
            "name": "git_merge_back",
            "description": (
                "Merge an agent worktree branch into the main workspace. "
                "Refuses and reports when the branch has no new commits, "
                "uncommitted target-workspace changes overlap files the merge "
                "would update, or a conflict occurs. Conflicts are aborted; "
                "user work is never stashed or overwritten."
            ),
            "parameters": {
                "type": "object",
                "properties": {"branch": {"type": "string"}},
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_push",
            "description": (
                "Pushes the current branch to its remote; sets upstream if "
                "needed. In a session worktree this is the agent branch, not "
                "the primary branch."
            ),
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
            "name": "spawn_agent",
            "description": (
                "Delegate a self-contained piece of work to a sub-agent: a "
                "nested agent with its own fresh context that runs the task "
                "independently and returns its final message as this tool's "
                "result. The sub-agent sees ONLY the prompt you pass — "
                "include file paths, error messages, and decisions it needs. "
                "Launch several spawn_agent calls in the same turn to run "
                "them in parallel (max 4 at once). Use for: isolated "
                "research (explore), parallel independent subtasks, or work "
                "whose intermediate steps would bloat this conversation. "
                "Do NOT use for small tasks that need this conversation's "
                "context, or anything requiring a user decision mid-task — "
                "sub-agents cannot ask the user questions. Say one line "
                "about what you're delegating and why BEFORE the call — "
                "the user is watching and an unannounced spawn reads as a "
                "hang."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "description": (
                            "Which sub-agent to run: 'general-purpose' (all "
                            "tools) or 'explore' (read-only research), or a "
                            "user-defined agent name."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Fully self-contained task description for the "
                            "sub-agent. It sees nothing else from this "
                            "conversation."
                        ),
                    },
                },
                "required": ["agent_type", "prompt"],
            },
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
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": (
                "Save a durable fact to your persistent per-project memory "
                "(user preferences, feedback on how to work, project "
                "constraints not derivable from the code, resource "
                "pointers). The index of saved memories is in your system "
                "prompt every turn. Update an existing memory by saving "
                "with its name; do not create near-duplicates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Kebab-case slug, e.g. 'prefers-dark-ui' or "
                            "'release-pipeline-gotchas'. Reuse the existing "
                            "slug when updating."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Short human title, e.g. 'Prefers dark UI'.",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "One-line summary shown in the index — what a "
                            "future you needs to decide relevance."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "One of: user | feedback | project | reference "
                            "(default project)."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "The memory itself, in markdown.",
                    },
                },
                "required": ["name", "title", "description", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_read",
            "description": (
                "Read one saved memory file in full (the system prompt "
                "only carries the one-line index entries). Read before "
                "updating an existing memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The memory's slug."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": (
                "Delete a saved memory that is wrong or obsolete and "
                "remove its index line."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The memory's slug."},
                },
                "required": ["name"],
            },
        },
    },
]


# ---------------------------------------------------------------- executors

MAX_BASH_TIMEOUT = 900
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
    # The killed tree may never deliver stdout EOF (write handles died
    # unread), so the pipe transports stay open with a read pending. Left
    # to the GC they __del__ after the loop is closed and raise
    # RuntimeError('Event loop is closed') as a
    # PytestUnraisableExceptionWarning (#82) — close them here, while a
    # live loop can still service the close.
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            transport.close()
        except Exception:  # noqa: BLE001 — already closed is fine
            pass


def _clamp_note(requested: int) -> str | None:
    # A silent clamp reads like a hung or flaky run and invites endless
    # re-runs; the model must hear that its ask was cut and how to cope.
    if int(requested or 60) > MAX_BASH_TIMEOUT:
        return (
            f"[requested timeout {int(requested)}s clamped to {MAX_BASH_TIMEOUT}s; "
            "if the command still doesn't fit, run it in chunks (per directory "
            "or file) and inspect incrementally rather than retrying whole]"
        )
    return None


async def _run_capturing(
    proc: asyncio.subprocess.Process, job, timeout: int, on_chunk=None
) -> tuple[str, bool]:
    """Read a spawned proc's merged stdout to completion under an overall
    deadline, invoking on_chunk(text) per decoded piece for live progress.
    Returns (output, timed_out). On timeout the tree is killed; the read
    stream is then dead, so wait() — not a second read — gets the exit."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    pieces: list[str] = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text and on_chunk is not None:
                try:
                    on_chunk(text)
                except Exception:  # noqa: BLE001 — progress must never kill the tool
                    pass
            pieces.append(text)
        text = decoder.decode(b"", final=True)
        if text:
            pieces.append(text)
        # EOF only means the pipe closed; wait() fills in returncode.
        await asyncio.wait_for(proc.wait(), timeout=5)
        return "".join(pieces), False
    except asyncio.TimeoutError:
        _kill_tree(proc, job)
        # The read was cancelled mid-stream; read() again is unsupported and
        # can hang forever. wait() only needs the exit.
        await _reap(proc)
        return f"[timed out after {timeout}s]", True
    except asyncio.CancelledError:
        # Run stopped mid-tool (#82): without this, nobody kills or reaps
        # the proc — its transport is later GC'd after the loop closed and
        # its __del__ raises RuntimeError('Event loop is closed') as a
        # PytestUnraisableExceptionWarning (red annotation on green CI).
        _kill_tree(proc, job)
        await _reap(proc)
        raise
    except Exception:
        # Any other unwind (broken pipe mid-read, ...) must also leave no
        # abandoned transport behind (#82).
        _kill_tree(proc, job)
        await _reap(proc)
        raise


async def _create_bash_process(
    command: str, cwd: Path, env: dict, *, windows: bool | None = None
):
    """Start the command in Git Bash on Windows when its wrapper is available."""
    windows = os.name == "nt" if windows is None else windows
    if windows:
        bash = resolve_git_bash(env)
        if bash:
            try:
                return await asyncio.create_subprocess_exec(
                    bash, "-c", command,
                    cwd=cwd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    **_NO_WINDOW, **_NEW_SESSION,
                )
            except OSError:
                # Only startup failure may fall back. Never rerun a failed
                # user command under a different shell dialect.
                pass
    return await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **_NO_WINDOW, **_NEW_SESSION,
    )


async def run_bash(
    workspace: str, command: str, timeout_seconds: int = 60, on_chunk=None
) -> dict:
    """Run a shell command in the workspace; return structured result.
    on_chunk, when given, receives output incrementally while it runs."""
    timeout = max(1, min(int(timeout_seconds or 60), MAX_BASH_TIMEOUT))
    note = _clamp_note(timeout_seconds)
    try:
        proc = await _create_bash_process(
            command, workspace_root(workspace), _tool_env()
        )
        job = _job_create()
        if job:
            _job_assign(job, proc.pid)
        try:
            output, timed_out = await _run_capturing(proc, job, timeout, on_chunk)
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
            **({"note": note} if note else {}),
        }
    except Exception as e:  # noqa: BLE001
        return {"exit_code": -1, "output": f"error: {e}", "timed_out": False, "truncated": False}


async def run_powershell(
    workspace: str, command: str, timeout_seconds: int = 60, on_chunk=None
) -> dict:
    """Run a Windows PowerShell command in the workspace; same structured
    result shape as run_bash. on_chunk receives output incrementally."""
    timeout = max(1, min(int(timeout_seconds or 60), MAX_BASH_TIMEOUT))
    note = _clamp_note(timeout_seconds)
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", command,
            cwd=workspace_root(workspace),
            env=_tool_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_NO_WINDOW, **_NEW_SESSION,
        )
        job = _job_create()
        if job:
            _job_assign(job, proc.pid)
        try:
            output, timed_out = await _run_capturing(proc, job, timeout, on_chunk)
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
            **({"note": note} if note else {}),
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
    # issue #58 R7: sibling agents' half-finished worktrees are search
    # noise — the model must never wade into another run's tree
    ".yaah",
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


async def search_conversation_history(
    workspace: str, conversation_id: int | None = None,
    query: str = "", max_results: int = 10,
) -> dict:
    """Search the active conversation's complete persisted text transcript."""
    if conversation_id is None:
        return {"error": "conversation history is unavailable in this context"}
    from backend.db.database import search_conversation_history as search

    return await search(conversation_id, query, max_results)


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


async def git_merge_back(workspace: str, branch: str) -> dict:
    """Integrate an `agent/*` branch into the primary workspace. Full
    refusal rules live in worktrees.merge_back: no new commits, overlapping
    uncommitted changes, and conflicts are reported without stashing or
    overwriting user work."""
    from backend.agent import worktrees as wt

    if not branch.strip():
        return {"error": "merge branch is empty"}
    root = wt.worktree_of(workspace) or workspace
    real = await wt.main_repo_root(root)
    if real is None:
        real = workspace_root(workspace)
    return await wt.merge_back(real, branch.strip())


async def git_push(workspace: str) -> dict:
    """Push the current branch. In a session worktree that is the agent
    branch, not the primary branch; callers that intend to publish main
    must target the main workspace explicitly."""
    r = await _git(workspace, "push")
    if r.get("exit_code") == 0:
        return r
    err = str(r.get("error") or "")
    if "has no upstream branch" not in err and "no upstream configured" not in err:
        return r
    rc, branch = await _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or not branch.strip() or branch.strip() == "HEAD":
        return r
    pushed = await _git(workspace, "push", "--set-upstream", "origin", branch.strip())
    if pushed.get("exit_code") == 0:
        pushed["note"] = f"no upstream was configured; published {branch.strip()} to origin with --set-upstream"
    return pushed


async def git_pull(workspace: str) -> dict:
    return await _git(workspace, "pull")


# ---------------------------------------------------------------- dispatch

from backend.agent.webtools import view_image, web_fetch, web_search


async def _install_git_executor(workspace: str) -> dict:
    from backend.agent import gitenv

    return await gitenv.run_install_git(workspace)


def _memory_executor(op: str):
    async def _run(workspace: str, **arguments) -> dict:
        from backend.agent import memory

        args = dict(arguments)
        if op == "save":
            # the schema's key is "type"; the module kwarg is mtype
            args["mtype"] = args.pop("type", None)
        fn = {
            "save": memory.save_memory,
            "read": memory.read_memory,
            "delete": memory.delete_memory,
        }[op]
        return fn(workspace, **args)

    return _run


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
    "search_conversation_history": search_conversation_history,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_add": git_add,
    "git_commit": git_commit,
    "git_merge_back": git_merge_back,
    "git_push": git_push,
    "git_pull": git_pull,
    "install_git": _install_git_executor,
    "get_help": get_help,
    "memory_save": _memory_executor("save"),
    "memory_read": _memory_executor("read"),
    "memory_delete": _memory_executor("delete"),
}

# Computer use (Q2: Windows-only hard line, same pattern as powershell but
# unconditional — non-Windows never imports this module, so the tools don't
# exist for the model there). The executors import pynput/mss lazily.
if os.name == "nt":
    from backend.agent.computer import COMPUTER_EXECUTORS, COMPUTER_HELP_DOCS, COMPUTER_TOOLS_SCHEMA

    TOOLS_SCHEMA += COMPUTER_TOOLS_SCHEMA
    EXECUTORS.update(COMPUTER_EXECUTORS)
    HELP_DOCS.update(COMPUTER_HELP_DOCS)

# Windows Sandbox tools (disposable test VMs + persistent dev toolkit,
# see backend/agent/sandbox.py): Windows-only for the same reason, and
# additionally stripped for remote sessions in get_schemas() — the sandbox
# integration drives THIS machine's VMs, never a remote host's.
_SANDBOX_NAMES: set = set()
if os.name == "nt":
    from backend.agent.sandbox import SANDBOX_EXECUTORS, SANDBOX_TOOLS_SCHEMA

    TOOLS_SCHEMA += SANDBOX_TOOLS_SCHEMA
    EXECUTORS.update(SANDBOX_EXECUTORS)
    _SANDBOX_NAMES = {s["function"]["name"] for s in SANDBOX_TOOLS_SCHEMA}

SCHEMAS = {s["function"]["name"]: s for s in TOOLS_SCHEMA}

# ---- access-mode classification -------------------------------------------
# Three risk classes drive the access-mode gate (see PLAN-access-modes.md):
#   read     - observation only; free in every mode
#   mutating - changes files inside/around the workspace; asks in "ask" mode
#   shell    - runs arbitrary commands / network+repo actions / MCP tools
# Anything not classified here defaults to "shell" (safe by default: an
# unknown or MCP tool always prompts under ask mode and blocks under plan).

_READ_TOOLS = {
    "read_file", "search_files", "git_status", "git_diff",
    "web_search", "web_fetch", "view_image", "load_skill",
    # observation-only computer-use tools (no input injection)
    "screenshot", "list_windows", "read_ui_tree", "wait",
    # pure observation: availability, enabled, session state
    "sandbox_status",
    # documentation lookup — reads only the schema set
    "get_help",
    # delegation is free in all modes: the sub-agent's own tool calls hit
    # the same gate, so spawning cannot launder permissions
    "spawn_agent",
    # conversation history is a read-only transcript query
    "search_conversation_history",
    # memory reads live outside the workspace but change nothing
    "memory_read",
}
_MUTATING_TOOLS = {
    "write_file", "edit_file", "create_file", "delete_file", "move_file",
    "git_add", "git_commit",
    # merges an agent worktree branch back into the shared tree (#58) —
    # mutating: it changes the target tree (refusal rules apply)
    "git_merge_back",
    # installs software on the host (silently, but gated: prompts in ask
    # mode, blocked in plan mode)
    "install_git",
    # creates a disposable VM and maps the workspace R/W into it (prompts
    # in ask mode, blocked in plan mode)
    "sandbox_test",
    # persistent memory lives outside the workspace (~/.yaah/memory) but
    # is model-authored content, so it gates like a file write
    "memory_save", "memory_delete",
}
_SHELL_TOOLS = {
    "bash", "powershell", "git_push", "git_pull",
    # computer-use control tools drive the real mouse/keyboard
    "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
    "type_text", "press_key", "focus_window",
    # arbitrary command execution inside the sandbox VM
    "sandbox_run", "sandbox_stop",
}


def tool_risk(name: str) -> str:
    """Classify a tool for the access-mode gate: "read" | "mutating" |
    "shell". Unknown names (MCP tools, future tools) classify as "shell"
    so the safe-by-default rule holds without maintaining a list."""
    if name in _READ_TOOLS:
        return "read"
    if name in _MUTATING_TOOLS:
        return "mutating"
    return "shell"


# Tools whose result already carried an error-nudge this session, so the
# hint is appended once per tool, not on every failure.
_NUDGED: set = set()


def _with_help_nudge(name: str, result: dict) -> dict:
    """Append a one-line get_help hint to a tool's error result, once per
    tool per session. The lazy docs tier is useless if the model never
    opens it - an error is the moment it's most likely to help."""
    if name in _NUDGED or name == "get_help":
        return result
    _NUDGED.add(name)
    result["error"] = (
        str(result.get("error", ""))
        + " (Hint: call get_help('%s') for usage notes.)" % name
    )
    return result


async def execute_tool(
    name: str, arguments: dict, workspace: str, on_chunk=None,
    conversation_id: int | None = None,
) -> dict:
    """Execute a tool by name with a dict of arguments. Never raises.

    While a remote session is active, workspace-touching tools are
    forwarded to the host (see backend/agent/remote.py); everything else
    runs locally. on_chunk, when given, is forwarded to the local shell
    executors for incremental output (remote/MCP tools ignore it).

    Issue #98 / adr/0002: write-provenance seeding. Before the call, shell
    redirection targets in the command are registered (the harness, not the
    model, writes those capture files); after it, file-tool writes and any
    output path a tool result reports are registered as model-authored.
    The registry feeds the merge-back trash classifier only — it is never
    consulted for access control."""
    from backend.agent import remote as remote_mod

    namespaced = remote_mod.parse_ns(workspace)
    if namespaced is not None and name in remote_mod.REMOTE_TOOLS:
        host = remote_mod.get_remote(namespaced[0])
        if host is None:
            return {"error": "no connected remote device owns this workspace"}
        return await host.exec_tool(name, arguments, workspace=workspace)
    if namespaced is None and name in remote_mod.REMOTE_TOOLS:
        # Legacy connected mode remains supported during the API/UI migration.
        host = remote_mod.get_remote()
        if host is not None:
            return await host.exec_tool(name, arguments, workspace=workspace)
    # MCP server tools route by name prefix, before the static executor map.
    if name.startswith("mcp_"):
        from backend.agent import mcp_client

        return await mcp_client.manager.call(name, arguments)
    fn = EXECUTORS.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}. Available: {sorted(EXECUTORS)}"}
    if name == "search_conversation_history":
        arguments = {**arguments, "conversation_id": conversation_id}
    # --- provenance seeding (pre-call) -------------------------------------
    try:
        from backend.agent import worktrees as _wt

        if name in ("bash", "powershell"):
            _wt.note_shell_writes(workspace, str(arguments.get("command") or ""))
    except Exception:  # noqa: BLE001 — provenance must never gate a tool
        pass
    try:
        if on_chunk is not None and name in ("bash", "powershell"):
            result = await fn(workspace=workspace, on_chunk=on_chunk, **arguments)
        else:
            result = await fn(workspace=workspace, **arguments)
    except TypeError as e:
        return _with_help_nudge(name, {"error": f"Bad arguments for {name}: {e}"})
    except Exception as e:  # noqa: BLE001
        return _with_help_nudge(name, {"error": f"{type(e).__name__}: {e}"})
    # --- provenance recording (post-call) ----------------------------------
    try:
        from backend.agent import worktrees as _wt

        if name in ("write_file", "create_file", "edit_file"):
            path = arguments.get("path")
            if path:
                _wt.note_write(workspace, str(path), "model")
        else:
            # Only explicit save-target keys — NOT `path` (read_file
            # returns it, and a read is not a write).
            for key in ("file", "saved", "written"):
                value = result.get(key)
                if isinstance(value, str) and "/" in value:
                    _wt.note_write(workspace, value, "model")
    except Exception:  # noqa: BLE001
        pass
    return result


def get_schemas(workspace: str | None = None) -> list:
    """Return schemas for the actual execution target.

    A namespaced workspace selects its own host. The legacy active connection
    remains a fallback only for callers that do not provide a workspace.
    """
    from backend.agent import remote as remote_mod

    remote_target = workspace is not None and remote_mod.parse_ns(workspace) is not None
    host = (
        remote_mod.remote_for_workspace(workspace)
        if remote_target
        else remote_mod.get_remote()
    )
    windows = host.windows if host is not None else (os.name == "nt" and not remote_target)
    schemas = TOOLS_SCHEMA + [POWERSHELL_SCHEMA] if windows else TOOLS_SCHEMA
    if remote_target and host is None:
        # Never expose local-only host-computer tools when an explicitly
        # selected remote host is unavailable; execution will fail closed.
        _local_computer_names = {
            "screenshot", "list_windows", "read_ui_tree", "focus_window",
            "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
            "type_text", "press_key", "wait", "sandbox_test", "sandbox_run",
            "sandbox_status", "sandbox_stop",
        }
        schemas = [
            s for s in schemas
            if s["function"]["name"] not in _SANDBOX_NAMES
            and s["function"]["name"] not in _local_computer_names
        ]
    # install_git installs on THIS machine with the client's bundled
    # installer, so it's only offered in local sessions when git is
    # actually missing and the installer shipped in this build.
    if host is None and os.name == "nt" and not remote_target:
        from backend.agent import gitenv

        if not gitenv.find_git() and gitenv.find_installer():
            schemas = schemas + [INSTALL_GIT_SCHEMA]
    # Sandbox tools drive the LOCAL machine's Windows Sandbox (they're in
    # TOOLS_SCHEMA only when os.name == "nt"): strip them whenever a remote
    # host is connected — a remote Windows host must not see the client's
    # sandbox any more than a Linux one should.
    if host is not None or remote_target:
        schemas = [s for s in schemas
                   if s["function"]["name"] not in _SANDBOX_NAMES]
        if remote_target:
            schemas = [
                s for s in schemas
                if s["function"]["name"] not in {
                    "screenshot", "list_windows", "read_ui_tree", "focus_window",
                    "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
                    "type_text", "press_key", "wait",
                }
            ]
    # MCP server tools (mcp_<server>_<tool>) merge in dynamically — they're
    # client-local like web/ask_user, regardless of where file tools run.
    from backend.agent import mcp_client

    schemas = schemas + mcp_client.manager.schemas()
    return schemas
