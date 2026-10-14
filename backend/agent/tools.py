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
import signal
import subprocess
from pathlib import Path

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

# Shared never-shell-background warning - one canonical sentence (kill-tree
# since _kill_tree: backgrounded children are killed at timeout, losing
# their work, not wedged forever). Both shell schemas embed it.
_BG_WARN = (
    "Never shell-background a long-running process (trailing &, start /b): "
    "backgrounded children are killed at timeout, losing their work. "
    "Servers and watchers need a detached spawn "
)

# Issue #187 (SYN-13): the git-editor rule has exactly one rendered home —
# the bash schema description. Other tool schemas carry only a short
# pointer (the sandbox prompt section keeps only the VM-specific
# GIT_EDITOR-preset delta).
_GIT_EDITOR_NOTE = (
    "git must never open its editor: pass -m '<message>' to git commit "
    "and use GIT_EDITOR=true for git rebase --continue / tag -a / "
    "commit --amend - an interactive editor blocks the tool until it "
    "times out."
)
_GIT_EDITOR_POINTER = (
    "git must never open its editor - see the bash tool note for the "
    "exact recipe."
)

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command in the workspace directory. Use for "
                "builds, tests, git, file discovery (ls, grep, find), and text "
                "processing. Long-running commands will time out. "
                + _BG_WARN
                + "instead (e.g. Start-Process with redirect, or nohup with "
                "stdout/stderr redirected to a file). For a test suite longer "
                "than the timeout cap, run it in chunks (per directory or "
                "file) instead of one monolithic run. "
                # Issue #187 (SYN-13): the git-editor rule is canonical here,
                # shared verbatim with the powershell schema.
                + _GIT_EDITOR_NOTE
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (1-900, default 60)",
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
            "time out. " + _BG_WARN +
            "instead (Start-Process, optionally -WindowStyle Hidden). "
            # Issue #187 (SYN-13): pointer copy — the full recipe lives in
            # the bash schema description only.
            + _GIT_EDITOR_POINTER
        ),
        "parameters": {
            "type": "object",
            "properties": {
                    "command": {"type": "string", "description": "The PowerShell command to run"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (1-900, default 60)",
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
            "PATH is picked up per call - if a git command still fails, "
            "retry it once."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

# ---- shared prompt-surface constants (#181) -------------------------------
# Single-source texts interpolated wherever the same guidance was previously
# hand-maintained in several places that could (and did) drift apart.

# Delegation policy: one phrasing, interpolated into the spawn_agent schema
# description, HELP_DOCS['spawn_agent'], and the sub-agents index block.
DELEGATION_POLICY = (
    "Sub-agents see ONLY the prompt you pass \u2014 include file paths, "
    "error messages, and decisions they need; they cannot ask the user "
    "questions. Launch several in the same turn to run them in parallel "
    "(max 4 at once; extra calls queue). Do not delegate work that needs "
    "this conversation's context or a user decision mid-task."
)


def tool_prose_list(names, annotations: dict | None = None) -> str:
    """Render the 'You have tools: ...' sentence from the REAL tool set.

    `names` is an iterable of tool-name strings or schema dicts (anything
    with ['function']['name']); `annotations` maps bare tool names to a
    short parenthetical. Callers pass the same list they hand to the
    model (loop.py) or the resolution of _resolve_tools (subagents.py),
    so the prose can never drift from the schemas. #181
    """
    annotations = annotations or {}
    parts = []
    for item in names:
        name = item["function"]["name"] if isinstance(item, dict) else item
        note = annotations.get(name)
        parts.append(f"{name} ({note})" if note else name)
    return f"You have tools: {', '.join(parts)}."


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
        "killed - output captured before the deadline is returned, "
        "followed by a [timed out after Ns] marker."
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
        "site, add site:example.com to the query. Fails visibly: an "
        "anomaly-modal challenge, an empty result page, or a 'search "
        "request failed' error - rephrase or wait rather than "
        "hammering retries."
    ),
    "web_fetch": (
        "Renders the page in a real headless browser, so JS-heavy and "
        "bot-guarded sites usually work, but it is slow (~seconds) - "
        "do not use it for plain static files (use bash/powershell "
        "curl or read_file for local paths). Returns readable text "
        "plus an 'IMAGES ON PAGE' list (first 5 image URLs) for "
        "view_image. Blocked pages: fall back to web_search snippets - "
        "don't hammer the same URL (the tool already tries three "
        "routes; one delayed retry is reasonable, a loop is not). "
        "max_chars truncates from the top - fetch a "
        "specific anchor or raise the cap for long pages."
    ),
    "generate_image": (
        "Text-to-image on the stable-diffusion.cpp server configured in "
        "Settings (Image generation). The image is saved into the "
        "workspace under ai-images/ and attached to the conversation, so "
        "the user sees it inline; it is NOT sent back to you - call "
        "view_image on the saved file only if you must inspect it (e.g. "
        "the user asks to change something specific about it). "
        "RESOLUTION POLICY: the diffusion model always renders at the "
        "server's default size - that is the size its operator's "
        "hardware handles comfortably, and asking a large model for "
        "1024+ native can stall it for minutes under VRAM thrash. "
        "Request any width/height you like: it renders at the default "
        "(aspect-fitted) and reaches your size with the ESRGAN upscaler "
        "automatically; the result reports exactly what happened. "
        "negative_prompt and seed pass through. Generation runs as a "
        "server job: pressing Stop cancels it and returns immediately. "
        "Remote (namespaced) workspaces save to the store only."
    ),
    "upscale_image": (
        "Detail-preserving enlargement of an EXISTING image via the "
        "sd-server's native ESRGAN endpoint (POST /sdcpp/v1/upscale - "
        "no diffusion, fast, synchronous). Pass path (workspace path "
        "like ai-images/x.png from generate_image, or a store rel) plus "
        "either repeats (1-4, each repeat multiplies the size) or "
        "target_width/target_height (repeats are inferred). The result "
        "is a NEW file (originals are never overwritten) shown inline. "
        "Requires an ESRGAN upscaler on the server (capabilities "
        "upscale=true); upscaling and generation share the server's "
        "context lock, so they never run simultaneously."
    ),
    "view_image": (
        "Attaches the image to the conversation so a vision-capable "
        "model sees it on the next model call - usually immediately "
        "after this tool result, within the same turn. Local paths "
        "resolve relative to the workspace root. Use it for "
        "screenshots, charts, renders and UI captures; not for "
        "binary formats the model cannot render."
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
        "old_text must match the file bytes exactly - strip the "
        "line-number prefix read_file adds to its output. For "
        "multiple edits to one file, chain several edit_file calls."
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
    "get_help": (
        "With no argument, lists every tool with its full one-line "
        "description. With a name, returns the tool's parameter "
        "schema plus extended usage notes (caveats, examples, "
        "failure modes) that are NOT in the short schema. Cheap and "
        "read-classified: call it before first use of an unfamiliar "
        "tool, or immediately after any tool errors."
    ),
    "spawn_agent": (
        # #181: same single DELEGATION_POLICY constant as the schema.
        DELEGATION_POLICY
        + " Announce each delegation to the user in one line."
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


BRANCH_SELECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "branch_select",
        "description": (
            "Switch the workspace to a branch, on the user's request. A "
            "plain checkout of the workspace: the branch is created first "
            "when it does not exist. Git's own refusals (uncommitted "
            "changes that would be clobbered, unknown branch, a branch "
            "checked out in another worktree) surface as the error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch name to switch to (created when missing)",
                },
                "create": {
                    "type": "boolean",
                    "description": "Create the branch when it does not exist (default true)",
                },
            },
            "required": ["branch"],
        },
    },
}


async def branch_select(
    workspace: str = "",
    branch: str = "",
    create: bool = True,
    conversation_id: int | None = None,
    on_chunk=None,
) -> dict:
    """The direct-world switch (#359/ADR-0017): one plain checkout of the
    workspace on the named branch, creating the branch first when asked.
    No pins, no per-chat branch identity, no guarded flip - git's own
    refusals are the guard, and nothing is stored anywhere."""
    from backend.agent import gitinfo

    branch = (branch or "").strip()
    if not branch or branch.startswith("-"):
        return {"ok": False, "error": "branch must be a local branch name"}
    from backend.agent.remote import parse_ns

    if parse_ns(workspace) is not None:
        # remote:<host>:<path> namespaces have no local checkout to
        # switch; the host tree is switched with plain git on the host.
        return {"ok": False, "error": "remote workspace: switch the branch on the host checkout"}
    try:
        root = workspace_root(workspace)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    exists = branch in await gitinfo.list_local_branches(root)
    if not exists and not create:
        return {"ok": False, "error": f"not a local branch: {branch}"}
    if not exists:
        # Created from the current HEAD - git's own default start point;
        # no per-chat start-point rule survives the direct world.
        rc, out = await gitinfo._run_git(root, "branch", branch)
        if rc != 0:
            return {"ok": False, "error": (out or "").strip() or "branch creation failed"}

    # The switch itself. Git's refusals surface verbatim as the error:
    # a checkout that would clobber uncommitted work, an unknown branch,
    # or a branch checked out in another registered worktree.
    rc, out = await gitinfo._run_git(root, "checkout", branch)
    if rc != 0:
        return {"ok": False, "error": (out or "").strip() or "checkout failed"}
    gitinfo.invalidate_git_caches(root)
    return {"ok": True, "branch": branch, "created": not exists}


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


TOOLS_SCHEMA += [GET_HELP_SCHEMA, BRANCH_SELECT_SCHEMA]

# #346: tools keyed to the LOGICAL workspace. The run resolves the
# canonical memory key ONCE, from the same workspace string the prompt's
# injected index reads, and the funnel injects it here - so a memory tool
# call and the prompt block can never disagree about the store (the run
# no longer rebinds its tool workspace; the direct world has one tree).
# One spelling, shared by the funnel gate, the loop's kwarg feed and the
# sub-agent runner:
MEMORY_TOOLS = ("memory_save", "memory_read", "memory_search", "memory_delete")

# Issue #169: the persistent-memory tool names, used by the Settings toggle
# (memory.enabled, default OFF) to filter the schema and gate execution.
# Derived from MEMORY_TOOLS so the two can never drift.
_MEMORY_TOOL_NAMES = set(MEMORY_TOOLS)

# #303: tools whose execution is scoped to the calling chat. The harness
# injects conversation_id at the single dispatch funnel (execute_tool);
# the model never passes it, and the executor signatures take it
# optionally so direct calls (tests, other harness code) still work.
CONTEXT_TOOLS = ("search_conversation_history",)


def memory_workspace_for(workspace: str | None) -> str | None:
    """The canonical memory key input for `workspace`: the workspace
    string itself. Non-empty local paths and remote namespaces pass
    through untouched (remote keeps its raw ``remote:<host>:<path>``
    form - memories stay client-local by design). Blank/Default (`''`
    or `'.',` the home pseudo-workspace) returns None: those can never
    be a real store, so the executor's fallback to the call workspace
    is exact. Direct local callers (tests, other harness code) resolve
    from the call workspace as they always have."""
    ws = (workspace or "").strip()
    if not ws or ws == ".":
        return None
    return ws

TOOLS_SCHEMA += [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web with DuckDuckGo (free, no API key). Returns "
                "a numbered list of title / URL / snippet. Use web_fetch to "
                "read a result in full. Prefer this over inferring from code "
                "alone whenever a fact about external software may already "
                "be documented: library/API behavior, error messages, config "
                "formats, version compatibility, third-party quirks. A "
                "couple of searches is cheaper than a long inference chain "
                "from first principles. Facts about this workspace's private "
                "code, user-local state, or secrets are not on the internet "
                "- searching for those wastes turns."
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
                "If blocked, fall back to web_search snippets - don't "
                "hammer the same URL. web_fetch lists the page's first "
                "5 image URLs under 'IMAGES ON PAGE' for view_image."
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
            "name": "spawn_agent",
            "description": (
                # #181: policy text is the single DELEGATION_POLICY
                # constant, also interpolated into HELP_DOCS and the
                # sub-agents index.
                DELEGATION_POLICY
                + " Use for: isolated research (explore), parallel "
                "independent subtasks, or work whose intermediate steps "
                "would bloat this conversation. Say one line about what "
                "you're delegating and why BEFORE the call — the user "
                "is watching and an unannounced spawn reads as a hang."
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
                            "(default project; unknown values are coerced "
                            "to project)."
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
            "name": "memory_search",
            "description": (
                "List saved memories matching a query, across the whole "
                "store — including entries trimmed out of the prompt "
                "index when it exceeded its cap (their slug is otherwise "
                "undiscoverable)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring to match against "
                            "each memory's content."
                        ),
                    },
                },
                "required": ["query"],
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
        # #180: output captured before the deadline is real; return it ahead
        # of the marker instead of discarding it. Flush the decoder first so
        # bytes buffered mid-UTF-8-character get the errors="replace"
        # treatment (CodeRabbit return trip on PR #221) instead of being
        # silently dropped.
        tail = decoder.decode(b"", final=True)
        return "".join(pieces) + tail + f"\n[timed out after {timeout}s]", True
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


async def _run_shell(
    spawn, workspace: str, command: str, timeout_seconds, on_chunk
) -> dict:
    """Shared spawn/capture/truncate body of run_bash/run_powershell.
    `spawn` builds the (already-configured) subprocess."""
    # Fractional timeouts are accepted (floored at 0.1) so tests can wait
    # out a real deadline in milliseconds; the schema stays integer.
    timeout = max(0.1, min(float(timeout_seconds or 60), MAX_BASH_TIMEOUT))
    note = _clamp_note(timeout_seconds)
    try:
        proc = await spawn(command)
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


async def run_bash(
    workspace: str, command: str, timeout_seconds: int = 60, on_chunk=None
) -> dict:
    """Run a shell command in the workspace; return structured result.
    on_chunk, when given, receives output incrementally while it runs."""

    async def spawn(cmd: str):
        return await _create_bash_process(cmd, workspace_root(workspace), _tool_env())

    return await _run_shell(spawn, workspace, command, timeout_seconds, on_chunk)


async def run_powershell(
    workspace: str, command: str, timeout_seconds: int = 60, on_chunk=None
) -> dict:
    """Run a Windows PowerShell command in the workspace; same structured
    result shape as run_bash. on_chunk receives output incrementally."""

    async def spawn(cmd: str):
        return await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", cmd,
            cwd=workspace_root(workspace),
            env=_tool_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_NO_WINDOW, **_NEW_SESSION,
        )

    return await _run_shell(spawn, workspace, command, timeout_seconds, on_chunk)


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

# ---------------------------------------------------------------- dispatch

from backend.agent.webtools import view_image, web_fetch, web_search


async def _install_git_executor(workspace: str) -> dict:
    from backend.agent import gitenv

    return await gitenv.run_install_git(workspace)


def _memory_executor(op: str):
    """The memory_save / memory_read / memory_delete executor factory.

    `memory_workspace` (#346) is the harness-injected canonical key (see
    memory_workspace_for): present on every dispatched call, None for
    direct local callers, which keep resolving from `workspace` as
    before."""
    from backend.agent import memory

    async def _run(
        workspace: str, memory_workspace: str | None = None, **arguments
    ) -> dict:
        args = dict(arguments)
        if op == "save":
            # the schema's key is "type"; the module kwarg is mtype
            args["mtype"] = args.pop("type", None)
        fn = {
            "save": memory.save_memory,
            "read": memory.read_memory,
            "search": memory.search_memory,
            "delete": memory.delete_memory,
        }[op]
        canonical = memory_workspace or workspace
        return fn(canonical, **args)

    return _run


# ---------------------------------------------------------------- image gen
# Text-to-image via a stable-diffusion.cpp server (sd-server, default
# http://127.0.0.1:1234). Two tools: generate_image (txt2img) and
# upscale_image (native ESRGAN endpoint over an existing image).
#
# Resolution policy (learned the hard way on a 12GB card): the DIFFUSION
# model always renders at the server's default size - asking a 9B flux
# model for 1024 native can thrash weights forever under VRAM pressure.
# Anything larger than the default is produced by the ESRGAN upscale
# chain (fast, detail-preserving, staged on the server's real scale
# factor). The harness hard-codes no resolution; the server's
# capabilities decide what "default" means.

_IMAGE_GEN_TOOLS = ("generate_image", "upscale_image")

GENERATE_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Generate an image from a text prompt with the local "
            "stable-diffusion.cpp server (set in Settings). Saves the PNG "
            "into the workspace under ai-images/ AND attaches it to this "
            "conversation so the user sees it inline. Use for logos, "
            "mockups, illustrations, art assets. The image is not sent "
            "back to you; view_image the saved file only if inspection "
            "is needed. The diffusion model renders at the server's "
            "default size; larger sizes are reached by upscaling "
            "automatically, and the result reports what actually happened."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "What to render. Describe subject, style, medium, "
                        "lighting, composition."
                    ),
                },
                "width": {
                    "type": "integer",
                    "description": (
                        "Desired image width in pixels. Bigger than the "
                        "server default is fine: it renders at the default "
                        "and upscales. Omit for the server default."
                    ),
                },
                "height": {
                    "type": "integer",
                    "description": (
                        "Desired image height in pixels. Same rule as "
                        "width. Omit for the server default."
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "What to avoid in the image.",
                },
                "seed": {
                    "type": "integer",
                    "description": "Seed for reproducible output.",
                },
            },
            "required": ["prompt"],
        },
    },
}

UPSCALE_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "upscale_image",
        "description": (
            "Upscale an existing image with the local stable-diffusion.cpp "
            "server's ESRGAN models (no re-generation - detail-preserving, "
            "fast). Use to enlarge a previously generated or saved image, "
            "e.g. when the user wants the same picture at a bigger size. "
            "The result is saved as a NEW file and shown to the user "
            "inline."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Image to upscale: a workspace path (e.g. "
                        "ai-images/x.png from generate_image) or the "
                        "store path from a tool result."
                    ),
                },
                "target_width": {
                    "type": "integer",
                    "description": (
                        "Desired result width; steps are inferred from "
                        "it. Omit to upscale by one step."
                    ),
                },
                "target_height": {
                    "type": "integer",
                    "description": "Desired result height (pairs with target_width).",
                },
                "repeats": {
                    "type": "integer",
                    "description": (
                        "Explicit 1-4: run the upscaler this many times. "
                        "Takes precedence over target_width/height."
                    ),
                },
                "upscaler": {
                    "type": "string",
                    "description": (
                        "Optional upscaler name from the server's "
                        "capabilities; omit to use the first available."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}

# Same posture as memory (#169) / sandbox (#340): if the image config
# disappears between schema build and tool call, refuse with a message
# that points at Settings and teaches no bypass.
_IMAGE_GEN_DISABLED_MSG = (
    "No image provider is configured. Only the user can set one up "
    "in Settings (Image generation)."
)

# Capabilities are small static-per-server metadata; cache per api_base so
# a chatty generation loop does not re-fetch every call. Short TTL: a
# server restart with different models/flags is noticed quickly.
_CAPS_CACHE: dict = {}
_CAPS_TTL = 300.0


def _png_dims(raw: bytes):
    """(width, height) of a PNG blob (IHDR sits at a fixed offset), or
    (None, None) for anything unparsable."""
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24:
        return (None, None)
    import struct

    w, h = struct.unpack(">II", raw[16:24])
    return (int(w), int(h))


async def _capabilities(api_base: str) -> dict:
    """GET /sdcpp/v1/capabilities, cached briefly. Returns {} on any
    failure - every caller must degrade to default behavior."""
    import time

    import httpx

    key = api_base.rstrip("/")
    now = time.monotonic()
    hit = _CAPS_CACHE.get(key)
    if hit and now - hit[0] < _CAPS_TTL:
        return hit[1]
    caps: dict = {}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(key + "/sdcpp/v1/capabilities")
        if r.status_code == 200:
            caps = r.json() or {}
    except Exception:  # noqa: BLE001 — any probe failure = default behavior
        caps = {}
    _CAPS_CACHE[key] = (now, caps)
    return caps


def _native_bounds(caps: dict) -> dict:
    """Size bounds + defaults + upscale availability from capabilities,
    with safe fallbacks matching sd-server defaults for older builds."""
    limits = caps.get("limits") or {}
    defaults = (caps.get("defaults") or {}) if isinstance(caps.get("defaults"), dict) else {}
    return {
        "min_w": int(limits.get("min_width") or 64),
        "min_h": int(limits.get("min_height") or 64),
        "max_w": int(limits.get("max_width") or 2048),
        "max_h": int(limits.get("max_height") or 2048),
        "def_w": int(defaults.get("width") or 512),
        "def_h": int(defaults.get("height") or 512),
        "upscale": bool(caps.get("upscale")),
        "max_up_w": int(limits.get("max_upscale_width") or 8192),
        "max_up_h": int(limits.get("max_upscale_height") or 8192),
    }


def _mult64(n: float) -> int:
    """Round down to a multiple of 64 (diffusion-friendly), floor at 64."""
    return max(64, (int(n) // 64) * 64)


def _clamp(v, lo, hi) -> int:
    return int(max(lo, min(hi, v)))


async def _img_gen_request(api_base: str, body: dict):
    """Legacy transport: sync POST /v1/images/generations (servers without
    the native job API). Returns (b64, error); exactly one."""
    import base64

    import httpx

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            r = await client.post(
                api_base.rstrip("/") + "/v1/images/generations", json=body
            )
        if r.status_code != 200:
            return None, (
                f"image generation failed: HTTP {r.status_code} from "
                f"{api_base} (is the stable-diffusion.cpp server running?)"
            )
        data = r.json().get("data") or []
        b64 = (data[0] or {}).get("b64_json") if data else None
        if not b64:
            return None, "image generation returned no image data"
        return b64, None
    except (httpx.HTTPError, ValueError) as e:
        return None, f"image generation failed: {type(e).__name__}: {e}"


async def _img_gen_job_submit(api_base: str, body: dict):
    """Native transport: POST /sdcpp/v1/img_gen -> 202 {id, poll_url}.
    Returns (job_id, error); exactly one."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(api_base.rstrip("/") + "/sdcpp/v1/img_gen", json=body)
        if r.status_code not in (200, 202):
            return None, f"job submission failed: HTTP {r.status_code} {str(r.json())[:200]}".rstrip()
        j = r.json() or {}
        job_id = j.get("id")
        if not job_id:
            return None, "job submission returned no job id"
        return job_id, None
    except (httpx.HTTPError, ValueError) as e:
        return None, f"job submission failed: {type(e).__name__}: {e}"


async def _job_poll(api_base: str, job_id: str):
    """GET /sdcpp/v1/jobs/{id}. Returns (status, result, error) where
    status is queued|generating|completed|failed|cancelled|gone and
    result/error are the server's payloads (either may be None)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{api_base.rstrip('/')}/sdcpp/v1/jobs/{job_id}")
        if r.status_code == 404 or r.status_code == 410:
            return "gone", None, None
        if r.status_code != 200:
            return "failed", None, {"code": "http", "message": f"HTTP {r.status_code}"}
        j = r.json() or {}
        return j.get("status") or "queued", j.get("result"), j.get("error")
    except (httpx.HTTPError, ValueError) as e:
        return "failed", None, {"code": type(e).__name__, "message": str(e)}


async def _job_cancel(api_base: str, job_id: str) -> None:
    """Best-effort POST /sdcpp/v1/jobs/{id}/cancel - removes QUEUED work
    server-side. A job already generating runs to completion server-side
    (the server has no preemption); cancelling still stops the harness
    from waiting on it. Never raises."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{api_base.rstrip('/')}/sdcpp/v1/jobs/{job_id}/cancel")
    except Exception:  # noqa: BLE001 — best effort by contract
        pass


async def _img_gen_job_wait(api_base: str, job_id: str, deadline_s: float = 600.0):
    """Poll a native img_gen job to completion. Returns (b64, error).
    On CancelledError (the user pressed Stop): best-effort server-side
    cancel, then re-raise so the run stops immediately instead of
    waiting out a thrashing server."""
    import asyncio
    import time

    start = time.monotonic()
    while True:
        status, result, error = await _job_poll(api_base, job_id)
        if status == "completed":
            images = (result or {}).get("images") or []
            b64 = (images[0] or {}).get("b64_json") if images else None
            if not b64:
                return None, "job completed but returned no image data"
            return b64, None
        if status in ("failed", "cancelled"):
            msg = (error or {}).get("message") or status
            return None, f"image generation job {status}: {msg}"
        if status == "gone":
            return None, "image generation job expired server-side before completion"
        if time.monotonic() - start > deadline_s:
            await _job_cancel(api_base, job_id)
            return None, (
                "image generation timed out after "
                f"{int(deadline_s)}s (job cancelled; the model may be "
                "thrashing - try a smaller size)"
            )
        await asyncio.sleep(1.0)


async def _img_upscale_request(api_base: str, b64: str, repeats: int, upscaler=None):
    """POST /sdcpp/v1/upscale (synchronous native endpoint, no job).
    Returns (raw_bytes, meta, error); exactly one of raw/meta is usable."""
    import base64

    import httpx

    body: dict = {"image": b64, "repeats": max(1, min(4, int(repeats)))}
    if upscaler:
        body["upscaler"] = upscaler
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            r = await client.post(
                api_base.rstrip("/") + "/sdcpp/v1/upscale", json=body
            )
        if r.status_code != 200:
            try:
                detail = str(r.json())[:200]
            except ValueError:
                detail = ""
            return None, None, f"upscale failed: HTTP {r.status_code} {detail}".rstrip()
        j = r.json()
        images = j.get("images") or []
        b = (images[0] or {}).get("b64_json") if images else None
        if not b:
            return None, None, "upscale returned no image data"
        meta = {
            "upscaler": j.get("upscaler"),
            "scale": j.get("scale"),
            "repeats": j.get("repeats"),
            "width": j.get("width"),
            "height": j.get("height"),
        }
        return base64.b64decode(b), meta, None
    except (httpx.HTTPError, ValueError) as e:
        return None, None, f"upscale failed: {type(e).__name__}: {e}"


def image_generation_configured() -> bool:
    """Is an image provider configured? Read live so a Settings save
    applies to new turns without a restart. Any read failure keeps the
    safe default (OFF)."""
    try:
        from backend.agent.config import load_config

        return bool((load_config().get("image_generation") or {}).get("api_base", "").strip())
    except Exception:  # noqa: BLE001 — fail closed, images are opt-in
        return False


def _read_image_argument(path: str, workspace: str):
    """Load image bytes for upscale_image: a workspace-relative path
    first, then a backend store rel. Returns bytes or an error string."""
    from backend.agent.imagedata import IMAGES_ROOT

    p = str(path or "").strip()
    if not p:
        return "path is required"
    try:
        from backend.agent.remote import parse_ns

        if parse_ns(workspace) is None:
            # resolve_path enforces workspace containment (the path is
            # model-supplied: without it, ../ or absolute paths would read
            # any file the backend can reach and ship it to the server).
            try:
                cand = resolve_path(workspace, p, for_write=False)
            except ValueError:
                cand = None
            if cand is not None and cand.is_file():
                return cand.read_bytes()
    except OSError:
        pass
    try:
        cand = (IMAGES_ROOT / p).resolve()
        if cand.is_file() and IMAGES_ROOT.resolve() in cand.parents:
            return cand.read_bytes()
    except OSError:
        pass
    return (
        f"image not found: {p} (pass the ai-images/... path from the "
        "generation result, or a workspace path)"
    )


async def _upscale_to_target(
    api_base: str,
    b64: str,
    want_w: int,
    want_h: int,
    upscaler: str | None,
    max_up_w: int = 8192,
    max_up_h: int = 8192,
):
    """Reach (want_w, want_h) by CHAINING single-repeat calls: the server
    reports each model's real scale factor in the response (x2 or x4
    models exist; assuming x2 overshoots badly on x4 and vice versa).
    Returns (raw, meta, note) - note is None or a plain-language summary
    of what happened. Stops at the target, after 4 steps, or before a
    step that would exceed the endpoint's output limit."""
    import base64

    cur = b64
    steps = 0
    last_meta: dict = {}
    note: str | None = None
    while True:
        raw, meta, err = await _img_upscale_request(api_base, cur, 1, upscaler)
        if err or raw is None:
            if steps == 0:
                return None, None, err
            note = f"stopped after {steps} upscale step(s): further upscaling failed ({err})"
            return base64.b64decode(cur), last_meta, note
        steps += 1
        cur = base64.b64encode(raw).decode()
        last_meta = meta or {}
        w = int(last_meta.get("width") or 0)
        h = int(last_meta.get("height") or 0)
        if w and h and w >= want_w and h >= want_h:
            return base64.b64decode(cur), last_meta, note
        scale = int(last_meta.get("scale") or 2)
        if w and h and (w * scale > max_up_w or h * scale > max_up_h):
            note = (
                f"stopped at {w}x{h}: another {scale}x step would exceed "
                f"the server's {max_up_w}x{max_up_h} upscale limit"
            )
            return base64.b64decode(cur), last_meta, note
        if steps >= 4:
            note = f"stopped after {steps} upscale step(s) at {w}x{h}"
            return base64.b64decode(cur), last_meta, note


async def generate_image(
    prompt: str,
    workspace: str,
    width: int | None = None,
    height: int | None = None,
    negative_prompt: str | None = None,
    seed: int | None = None,
) -> dict:
    """Run text-to-image on the configured sd-server. The DIFFUSION model
    always renders at the server's default size (a 9B flux model can
    thrash VRAM for minutes above it); requested sizes beyond the default
    are reached with the staged ESRGAN chain. Transport: the native async
    job API when capabilities are available (Stop cancels the queued job
    and stops waiting immediately), the sync OpenAI-compatible endpoint
    as the fallback for older servers."""
    import asyncio
    import base64

    from backend.agent.imagedata import save_bytes

    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "prompt must not be empty"}

    try:
        from backend.agent.config import load_config

        cfg = load_config().get("image_generation") or {}
    except Exception:  # noqa: BLE001
        return {"error": _IMAGE_GEN_DISABLED_MSG}
    api_base = (cfg.get("api_base") or "").strip()
    if not api_base:
        return {"error": _IMAGE_GEN_DISABLED_MSG}

    try:
        width = int(width) if width else None
        height = int(height) if height else None
    except (TypeError, ValueError):
        return {"error": "width/height must be integers"}

    caps = await _capabilities(api_base)
    b = _native_bounds(caps) if caps else None
    notes: list[str] = []
    up_target: tuple[int, int] | None = None
    body: dict = {"prompt": prompt}

    if b is None:
        # Older server without capabilities: legacy passthrough, its call.
        legacy: dict = {"prompt": prompt, "n": 1}
        if negative_prompt or seed is not None:
            extra: dict = {}
            if negative_prompt:
                extra["negative_prompt"] = negative_prompt
            if seed is not None:
                extra["seed"] = seed
            legacy["prompt"] = (
                prompt
                + " <sd_cpp_extra_args>"
                + json.dumps(extra)
                + "</sd_cpp_extra_args>"
            )
        if width or height:
            legacy["size"] = f"{width}x{height or width}"
        b64, err = await _img_gen_request(api_base, legacy)
        if err:
            return {"error": err}
        raw_out = base64.b64decode(b64)
        dims = _png_dims(raw_out)
        store_rel = _persist_generation(raw_out, workspace, notes)
        return _generation_result(
            store_rel, raw_out, dims, None, notes, workspace
        )

    # --- size decision (capability-aware, conservative) ------------------
    # Diffusion ALWAYS renders at the server default: that size is what
    # the server operator's model/VRAM comfortably handles (a 9B flux at
    # 1024 native can thrash weight paging for minutes). Requests bigger
    # than the default get an aspect-preserving default-size render plus
    # the staged ESRGAN chain up to the target.
    gen_w, gen_h = b["def_w"], b["def_h"]
    want_w = width or 0
    want_h = height or 0
    if want_w or want_h:
        # Keep the default's pixel budget (what this model/VRAM handles
        # comfortably) but match the requested aspect ratio.
        budget = float(b["def_w"]) * float(b["def_h"])
        ratio = (want_w or want_h) / max(want_h or want_w, 1.0)
        gw = max(64.0, (budget * ratio) ** 0.5)
        gh = max(64.0, gw / max(ratio, 0.0001))
        gen_w = _clamp(_mult64(gw), b["min_w"], b["max_w"])
        gen_h = _clamp(_mult64(gh), b["min_h"], b["max_h"])
        over = max(want_w / gen_w, want_h / gen_h, 1.0)
        if over > 1.0 and b["upscale"]:
            up_target = (
                _clamp(int(want_w or want_h), 64, b["max_up_w"]),
                _clamp(int(want_h or want_w), 64, b["max_up_h"]),
            )
        elif over > 1.0:
            notes.append(
                f"requested {want_w}x{want_h}: rendered at {gen_w}x{gen_h} "
                "(no ESRGAN upscaler is loaded on the server, so it "
                "cannot be enlarged)"
            )
        else:
            notes.append(
                f"requested {want_w}x{want_h}: rendered at the server "
                f"default {gen_w}x{gen_h}"
            )
    if width or height:
        # native bodies take the explicit size; when the user gave none we
        # omit it so the server default applies verbatim
        body["width"], body["height"] = gen_w, gen_h
    if negative_prompt:
        body["negative_prompt"] = negative_prompt
    if seed is not None:
        body["seed"] = seed

    # --- transport: native job API (cancellable), sync fallback ---------
    job_id, err = await _img_gen_job_submit(api_base, body)
    if err:
        # native endpoint unavailable (older build): fall back to sync
        legacy = dict(body)
        legacy["n"] = 1
        legacy["size"] = f"{body.get('width', b['def_w'])}x{body.get('height', b['def_h'])}"
        legacy.pop("width", None)
        legacy.pop("height", None)
        legacy["prompt"] = body["prompt"] + (
            " <sd_cpp_extra_args>"
            + json.dumps(
                {k: body[k] for k in ("negative_prompt", "seed") if k in body}
            )
            + "</sd_cpp_extra_args>"
            if ("negative_prompt" in body or "seed" in body)
            else ""
        )
        b64, err2 = await _img_gen_request(api_base, legacy)
        if err2:
            return {"error": f"{err}; fallback also failed: {err2}"}
        raw_out = base64.b64decode(b64)
    else:
        try:
            b64, err = await _img_gen_job_wait(api_base, job_id)
        except asyncio.CancelledError:
            await _job_cancel(api_base, job_id)
            raise
        if err:
            return {"error": err}
        raw_out = base64.b64decode(b64)
    dims = _png_dims(raw_out)

    if up_target:
        raw2, meta2, note2 = await _upscale_to_target(
            api_base,
            base64.b64encode(raw_out).decode(),
            up_target[0],
            up_target[1],
            None,
            max_up_w=b["max_up_w"],
            max_up_h=b["max_up_h"],
        )
        if raw2 is None:
            notes.append(
                f"requested {want_w}x{want_h}: upscale step failed ({note2}); "
                f"returning the {gen_w}x{gen_h} render"
            )
        else:
            raw_out = raw2
            dims = _png_dims(raw2)
            m = meta2 or {}
            notes.append(
                f"requested {want_w}x{want_h}: rendered {gen_w}x{gen_h} "
                f"(the model's default size), then upscaled via "
                f"{m.get('upscaler')}"
                + (
                    f" to {m.get('width')}x{m.get('height')}"
                    if m.get("width")
                    else ""
                )
                + (f"; {note2}" if note2 else "")
            )

    store_rel = _persist_generation(raw_out, workspace, notes)
    return _generation_result(store_rel, raw_out, dims, b, notes, workspace)


async def _save_upscale_output(out: dict, raw: bytes, workspace: str, source: str) -> dict:
    """Shared tail for upscale_image: workspace copy (remote sessions keep
    store-only) + friendly errors. Mutates and returns `out`."""
    try:
        from backend.agent.remote import parse_ns

        if parse_ns(workspace) is not None:
            out["note"] = (
                "workspace is a remote host; image saved to the local "
                "image store only (local-only in this version)"
            )
            return out
        dest = workspace_root(workspace) / "ai-images"
        dest.mkdir(parents=True, exist_ok=True)
        stem = (out["images"][0]).split("/")[-1]
        target = dest / f"upscaled-{stem}"
        target.write_bytes(raw)
        out["path"] = f"ai-images/{target.name}"
        out["saved_to"] = out["path"]
    except OSError as e:
        out["note"] = f"workspace copy failed: {e}"
    return out


async def upscale_image(
    path: str,
    workspace: str,
    repeats: int | None = None,
    target_width: int | None = None,
    target_height: int | None = None,
    upscaler: str | None = None,
) -> dict:
    """Upscale an existing image via the sd-server's native synchronous
    POST /sdcpp/v1/upscale (ESRGAN; no generation). Saves a NEW file in
    the workspace (ai-images/) and the chat store; the original is kept."""
    import base64

    from backend.agent.imagedata import save_bytes

    try:
        from backend.agent.config import load_config

        cfg = load_config().get("image_generation") or {}
    except Exception:  # noqa: BLE001
        return {"error": _IMAGE_GEN_DISABLED_MSG}
    api_base = (cfg.get("api_base") or "").strip()
    if not api_base:
        return {"error": _IMAGE_GEN_DISABLED_MSG}

    caps = await _capabilities(api_base)
    b = _native_bounds(caps) if caps else None
    if b is not None and not b["upscale"]:
        # Gate before touching the file: the server-side answer is the
        # actionable one (the user must load an ESRGAN model).
        return {
            "error": (
                "the server has no ESRGAN upscaler loaded (capabilities "
                "report upscale=false); the user needs to load one before "
                "images can be upscaled"
            )
        }

    raw_in = _read_image_argument(path, workspace)
    if isinstance(raw_in, str):
        return {"error": raw_in}
    dims_in = _png_dims(raw_in)

    if repeats is not None:
        try:
            n = max(1, min(4, int(repeats)))
        except (TypeError, ValueError):
            return {"error": "repeats must be an integer 1-4"}
    elif target_width or target_height:
        # Staged chain: each single-repeat response reports the model's
        # real scale; keep stepping until the target is met, the chain
        # cap, or the server's output limit. No x2-per-repeat guessing.
        cur_w, cur_h = dims_in[0] or 0, dims_in[1] or 0
        want_w = target_width or target_height or cur_w or 512
        want_h = target_height or target_width or cur_h or 512
        if cur_w and want_w <= cur_w and want_h <= cur_h:
            return {
                "error": (
                    f"image is already {cur_w}x{cur_h}, at or above the "
                    f"{want_w}x{want_h} target; upscaling cannot shrink it"
                )
            }
        raw_out, meta, note = await _upscale_to_target(
            api_base, base64.b64encode(raw_in).decode(), want_w, want_h,
            upscaler,
            max_up_w=b["max_up_w"] if b else 8192,
            max_up_h=b["max_up_h"] if b else 8192,
        )
        if raw_out is None:
            return {"error": note or "upscale returned no image"}
        out: dict = {
            "images": [save_bytes(raw_out, "png", "generate_image")],
            "display": "inline",
            "source": path,
            "steps": meta.get("repeats") if meta else None,
            "scale": meta.get("scale") if meta else None,
            "upscaler": meta.get("upscaler") if meta else None,
        }
        if note:
            out["note"] = note
        dims = _png_dims(raw_out)
        if dims and dims[0]:
            out["width"], out["height"] = dims
        return await _save_upscale_output(out, raw_out, workspace, path)

    else:
        n = 1

    raw_out, meta, err = await _img_upscale_request(
        api_base, base64.b64encode(raw_in).decode(), n, upscaler
    )
    if err or raw_out is None:
        return {"error": err or "upscale returned no image"}

    store_rel = save_bytes(raw_out, "png", "generate_image")
    out: dict = {
        "images": [store_rel],
        "display": "inline",
        "source": path,
        "repeats": n,
    }
    if meta:
        out.update({k: meta.get(k) for k in ("upscaler", "scale", "width", "height")})
    dims = _png_dims(raw_out)
    if dims and dims[0]:
        out["width"], out["height"] = dims
    return await _save_upscale_output(out, raw_out, workspace, path)


def _persist_generation(raw: bytes, workspace: str, notes: list[str]) -> str:
    """Save the final image to the backend store. Returns the store rel."""
    from backend.agent.imagedata import save_bytes

    return save_bytes(raw, "png", "generate_image")


def _generation_result(
    store_rel: str,
    raw: bytes,
    dims,
    b: dict | None,
    notes: list[str],
    workspace: str,
) -> dict:
    """Shared result assembly: inline display contract, dims, the server's
    bounds sentence, notes, and the workspace copy (remote sessions keep
    store-only)."""
    out: dict = {
        "images": [store_rel],
        "path": None,
        "saved_to": None,
        "display": "inline",
    }
    if dims and dims[0]:
        out["width"], out["height"] = dims
    if b:
        out["native"] = (
            f"the diffusion model renders at the server default "
            f"{b['def_w']}x{b['def_h']}; larger sizes are produced by the "
            "ESRGAN upscaler automatically (or call upscale_image on the "
            "saved file)"
        )
    if notes:
        out["note"] = " ".join(notes)
    try:
        from backend.agent.remote import parse_ns

        if parse_ns(workspace) is not None:
            # Q8 (design): remote workspaces are refused for v1 — bytes are
            # local, a written file would land on the wrong machine. The
            # image still displays; only the workspace copy is skipped.
            out["note"] = (
                out.get("note", "")
                + " workspace is a remote host; image saved to the local "
                "image store only (local-only in this version)"
            ).strip()
            return out
        rel_dir = "ai-images"
        dest = workspace_root(workspace) / rel_dir
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / (store_rel.split("/")[-1])
        target.write_bytes(raw)
        out["path"] = f"{rel_dir}/{target.name}"
        out["saved_to"] = out["path"]
    except OSError as e:
        out["note"] = (out.get("note", "") + f" workspace copy failed: {e}").strip()
    return out
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
    "install_git": _install_git_executor,
    "get_help": get_help,
    "branch_select": branch_select,
    "generate_image": generate_image,
    "upscale_image": upscale_image,
    "memory_save": _memory_executor("save"),
    "memory_read": _memory_executor("read"),
    "memory_search": _memory_executor("search"),
    "memory_delete": _memory_executor("delete"),
}

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
    "read_file", "search_files",
    "web_search", "web_fetch", "view_image", "load_skill",
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
    "memory_read", "memory_search",
}
_MUTATING_TOOLS = {
    "write_file", "edit_file", "create_file", "delete_file", "move_file",
    # installs software on the host (silently, but gated: prompts in ask
    # mode, blocked in plan mode)
    "install_git",
    # creates a disposable VM and maps the workspace R/W into it (prompts
    # in ask mode, blocked in plan mode)
    "sandbox_test",
    # persistent memory lives outside the workspace (~/.yaah/memory) but
    # is model-authored content, so it gates like a file write
    "memory_save", "memory_delete",
    # moves the workspace's checked-out branch (checkout + possible ref
    # creation) - a repo mutation, gated like a file write
    "branch_select",
}
_SHELL_TOOLS = {
    "bash", "powershell",
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
    memory_workspace: str | None = None,
) -> dict:
    """Execute a tool by name with a dict of arguments. Never raises.

    While a remote session is active, workspace-touching tools are
    forwarded to the host (see backend/agent/remote.py); everything else
    runs locally. on_chunk, when given, is forwarded to the local shell
    executors for incremental output (remote/MCP tools ignore it).

    memory_workspace (#346): the harness-resolved canonical memory key
    for the calling turn (its pre-rebind workspace root). None derives
    it from the call workspace — identical for non-pinned chats, and
    the same shape direct callers (tests, other harness code) had
    before the injection existed.

"""
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
    if name in _MEMORY_TOOL_NAMES and not memory_enabled():
        # Persistent memory is opt-in (#169): degrade gracefully if the
        # toggle flipped after the schemas were sent.
        return {
            "info": (
                "Persistent memory is currently disabled in Settings "
                "(General -> 'Enable persistent memory'). Re-enable it there "
                "if saving or reading project memories is needed."
            )
        }
    if name in _SANDBOX_NAMES and not sandbox_enabled():
        # Issue #340: same degrade-gracefully rule when the sandbox toggle
        # flipped after the schemas were sent — or when the agent has just
        # edited the config file to flip it back. Refuse and point at
        # Settings; crucially, the error must NOT name the config key or
        # file (the old message taught agents to bypass the toggle).
        return {"info": _SANDBOX_DISABLED_MSG}
    if name in _IMAGE_GEN_TOOLS and not image_generation_configured():
        # Same degrade-gracefully posture as memory/sandbox: the image
        # provider was unconfigured (or wiped) after the schemas were
        # sent. Refuse, point at Settings, name no config keys.
        return {"info": _IMAGE_GEN_DISABLED_MSG}
    if name in CONTEXT_TOOLS:
        # #303: one injection point for chat-scoped tools — the model can't
        # pass its own conversation id, and every dispatch path (direct,
        # gate-approved re-exec, sub-agent) funnels through execute_tool.
        arguments = {**arguments, "conversation_id": conversation_id}
    if name in _MEMORY_TOOL_NAMES:
        # #346: one injection point for the canonical memory key — the
        # run resolves it once per turn (pre-rebind) and every dispatch
        # path (direct, gate re-exec, sub-agent) funnels through here,
        # so a save and the prompt's injected index resolve the SAME
        # store. The model never passes it; an explicitly handed-down
        # key (sub-agent runner) wins over deriving from the call
        # workspace, and without either the executor falls back to the
        # call workspace (today's shape).
        arguments = {
            **arguments,
            "memory_workspace": memory_workspace
            or memory_workspace_for(workspace),
        }
    try:
        if on_chunk is not None and name in ("bash", "powershell"):
            result = await fn(workspace=workspace, on_chunk=on_chunk, **arguments)
        else:
            result = await fn(workspace=workspace, **arguments)
    except TypeError as e:
        return _with_help_nudge(name, {"error": f"Bad arguments for {name}: {e}"})
    except Exception as e:  # noqa: BLE001
        return _with_help_nudge(name, {"error": f"{type(e).__name__}: {e}"})
    return result


def memory_enabled() -> bool:
    """Issue #169: is persistent memory enabled? Global setting
    (memory.enabled), read live so a toggle applies to new turns without a
    restart. Any read failure keeps the safe default (OFF)."""
    try:
        from backend.agent.config import load_config

        return bool((load_config().get("memory") or {}).get("enabled", False))
    except Exception:  # noqa: BLE001 — fail closed, memory is opt-in
        return False


def sandbox_enabled() -> bool:
    """Issue #340: is the sandbox VM integration enabled? Global setting
    (sandbox.enabled), read live so a toggle applies to new turns without a
    restart. Any read failure keeps today's behavior (ON)."""
    try:
        from backend.agent.config import load_config

        return bool((load_config().get("sandbox") or {}).get("enabled", True))
    except Exception:  # noqa: BLE001 — fail open, never break a turn
        return True


# Issue #340: one refusal, shared by every sandbox entry point. Security
# copy must never drift — a "helpful" variant is how the original error
# ended up teaching agents the bypass recipe.
_SANDBOX_DISABLED_MSG = (
    "The Windows Sandbox is disabled in Settings. Only the user can "
    "re-enable it from the Settings window."
)


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
    # Image generation (sd-server) is a LOCAL-machine capability and a
    # main-chat-only tool (sub-agents exclude it): offered only while an
    # image provider is configured, and never in remote sessions — the
    # server runs on this machine, so remote workspaces refuse it (v1).
    if image_generation_configured() and host is None and not remote_target:
        schemas = schemas + [GENERATE_IMAGE_SCHEMA, UPSCALE_IMAGE_SCHEMA]
    # MCP server tools (mcp_<server>_<tool>) merge in dynamically — they're
    # client-local like web/ask_user, regardless of where file tools run.
    from backend.agent import mcp_client

    schemas = schemas + mcp_client.manager.schemas()
    # Issue #169: persistent memory is opt-in; until enabled the memory
    # tools never appear in the schema.
    if not memory_enabled():
        schemas = [
            s for s in schemas
            if s["function"]["name"] not in _MEMORY_TOOL_NAMES
        ]
    # Issue #340: same rule for the sandbox VM tools. When the user has
    # disabled the sandbox in Settings, the schemas never reach the model —
    # otherwise the model is invited to call a tool that must refuse, and
    # observant agents go looking for the switch instead.
    if not sandbox_enabled():
        schemas = [
            s for s in schemas if s["function"]["name"] not in _SANDBOX_NAMES
        ]
    return schemas
