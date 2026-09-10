"""Tool registry: schema definitions + safe execution.

Tools are the agent's hands. Each tool has an OpenAI-format JSON schema for
the model and an async executor. All paths are resolved against a workspace
root and validated to prevent escapes.
"""
import asyncio
import json
import shlex
from pathlib import Path


# ---------------------------------------------------------------- path safety

def resolve_path(workspace: str, rel_path: str) -> Path:
    """Resolve rel_path inside workspace; raise ValueError on escape."""
    root = Path(workspace).resolve()
    p = (root / rel_path).resolve()
    if not (p == root or root in p.parents):
        raise ValueError(f"Path escapes workspace: {rel_path}")
    return p


# ---------------------------------------------------------------- schemas

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command in the workspace directory. Use for "
                "builds, tests, git, file discovery (ls, grep, find), and text "
                "processing. Long-running commands will time out."
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
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset_line": {"type": "integer", "description": "1-based start line"},
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
]


# ---------------------------------------------------------------- executors

MAX_BASH_TIMEOUT = 300
MAX_OUTPUT_CHARS = 20_000


async def run_bash(workspace: str, command: str, timeout_seconds: int = 60) -> dict:
    """Run a shell command in the workspace; return structured result."""
    timeout = max(1, min(int(timeout_seconds or 60), MAX_BASH_TIMEOUT))
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = out.decode("utf-8", errors="replace")
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            output = f"[timed out after {timeout}s]"
            timed_out = True

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


async def read_file(workspace: str, path: str, offset_line: int = None, limit_lines: int = None) -> dict:
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

        start = (offset_line - 1) if offset_line and offset_line > 0 else 0
        if start >= total:
            return {"path": path, "total_lines": total, "lines": [], "content": ""}
        end = start + limit_lines if limit_lines and limit_lines > 0 else total
        window = lines[start:end]

        numbered = "\n".join(f"{start + i + 1:6d}\t{line}" for i, line in enumerate(window))
        return {
            "path": path,
            "total_lines": total,
            "start_line": start + 1,
            "content": numbered,
            "truncated": end < total,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def write_file(workspace: str, path: str, content: str) -> dict:
    """Write content to a file (create parent dirs); full overwrite."""
    p = resolve_path(workspace, path)
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
    p = resolve_path(workspace, path)
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


# ---------------------------------------------------------------- dispatch

EXECUTORS = {
    "bash": run_bash,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
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
    return TOOLS_SCHEMA
