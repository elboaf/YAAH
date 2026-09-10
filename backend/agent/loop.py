"""The agent loop: model <-> tools cycle with streaming events.

Runs until the model produces a final answer or the step budget is exhausted.
Emits JSON-line events for the frontend:
  {'type': 'text', 'text': ...}             - assistant text delta
  {'type': 'tool_start', 'name', 'args'}    - tool execution beginning
  {'type': 'tool_result', 'name', 'result'} - tool output
  {'type': 'done'}                          - final answer complete
  {'type': 'error', 'message'}              - fatal error
"""
import asyncio
import itertools
import json
import os
from typing import AsyncIterator

from backend.agent import model_client
from backend.agent.config import load_config
from backend.agent.imagedata import load_data_url
from backend.agent.tools import execute_tool, get_schemas
from backend.db.database import add_message, get_conversation, get_messages

DEFAULT_MAX_STEPS = 200
MAX_TOOL_RESULT_CHARS = 20_000

def _default_system_prompt() -> str:
    """SYSTEM_PROMPT adapted to the current OS: the tool list and the
    runtime-environment line must match what execute_tool can actually
    do here, or the model attempts commands for the wrong platform
    (e.g. PowerShell registry queries on Linux)."""
    import platform

    windows = os.name == "nt"
    tools = ["bash (shell commands)"]
    if windows:
        tools.append("powershell (Windows PowerShell)")
    tools += [
        "web_search", "web_fetch", "view_image", "read_file", "write_file",
        "create_file", "edit_file", "delete_file", "move_file",
        "search_files",
        "git tools (git_status, git_diff, git_add, git_commit, git_push, git_pull)",
    ]
    env = (
        f"Runtime environment: {platform.system()} {platform.release()} "
        f"({platform.machine()}). The bash tool runs commands through the "
        f"system shell ({'cmd.exe' if windows else 'bash/sh'}); use "
        f"commands and paths valid for THIS operating system."
    )
    return f"""You are an expert AI coding agent working inside a user's project workspace.

{env}

You have tools: {", ".join(tools)}.

Guidelines:
- Explore before acting: use search_files and read files before editing.
- Prefer edit_file for targeted changes; write_file only for new files or full rewrites.
- read_file returns line ranges: page through large files with start_line/end_line.
- Verify your work: run tests/builds via bash (or powershell for Windows-native
  tasks: registry, services, WMI) after changes when possible.
- For web research, start with web_search and read pages with web_fetch;
  use view_image on an image URL you actually need to see.
- Commit meaningful work with git_add/git_commit when the user asks for it.
- Be concise in prose; let tools do the talking.
- Paths are relative to the workspace root.

Interview the user (ask_user tool):
- Do not make assumptions about a plan, decision, or idea. Put each
  decision to the user with ask_user and wait for the answer.
- Finding facts is your job, never the user's: never ask for anything
  you could look up yourself with tools.
- Don't block on unsettled exploration: a running exploration is an
  unsettled prerequisite, so only the questions downstream of it wait
  for the exploration to report. Decisions wait; facts don't.
- The session is done when nothing is left silently assumed. Do not
  act on a decision until the user has confirmed shared understanding."""

# Per-conversation cancellation flags checked between model/tool steps.
_cancel_events: dict[int, asyncio.Event] = {}

# Pending ask_user calls: "conversation_id:call_id" -> Future carrying the
# user's answer text. Resolved by the /answer API endpoint.
_pending_answers: dict[str, asyncio.Future] = {}


def resolve_answer(conversation_id: int, call_id: str, answer: str) -> bool:
    """Deliver a user answer to a pending ask_user call. Returns False when
    no question is waiting (e.g. the run already ended or was stopped)."""
    fut = _pending_answers.get(f"{conversation_id}:{call_id}")
    if fut is None or fut.done():
        return False
    fut.set_result(answer)
    return True


async def _ask_user(
    conversation_id: int, call_id: str, args: dict, cancel_ev: asyncio.Event
) -> dict:
    """Block the loop until the user answers (or the run is cancelled).
    The stream stays open and other conversations keep running."""
    key = f"{conversation_id}:{call_id}"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_answers[key] = fut
    cancel_task = asyncio.create_task(cancel_ev.wait())
    try:
        done, _ = await asyncio.wait(
            {fut, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if fut in done:
            return {"answer": fut.result()}
        return {"answer": None, "note": "user did not answer (run stopped)"}
    finally:
        cancel_task.cancel()
        _pending_answers.pop(key, None)
        if not fut.done():
            fut.cancel()


def cancel_agent(conversation_id: int):
    """Request cancellation of a running agent turn for this conversation."""
    ev = _cancel_events.get(conversation_id)
    if ev is not None:
        ev.set()


def _cancelled(conversation_id: int) -> bool:
    ev = _cancel_events.get(conversation_id)
    return bool(ev and ev.is_set())


def _ndjson(event: dict) -> str:
    return json.dumps(event) + "\n"


def _image_part(data_url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": data_url}}


def _parts_with_images(text: str, image_rels: list) -> list | str:
    """Build an OpenAI multimodal parts list (text + image_url parts) from
    stored image rel paths. Falls back to plain text when none of the
    files can be read (deleted/moved)."""
    parts: list = []
    if text:
        parts.append({"type": "text", "text": text})
    for rel in image_rels:
        data_url = load_data_url(rel)
        if data_url:
            parts.append(_image_part(data_url))
        else:
            parts.append({"type": "text", "text": f"[image file missing: {rel}]"})
    return parts or text


async def load_history(conversation_id: int) -> list:
    """Load persisted messages back into OpenAI chat format."""
    rows = await get_messages(conversation_id)
    out = []
    # Track which assistant tool_call ids actually made it into the replayed
    # history, so we can drop orphaned 'tool' rows (e.g. when a malformed
    # assistant tool_calls payload was stripped above). Most OpenAI-compatible
    # APIs reject tool messages with no preceding assistant tool_calls.
    valid_call_ids: set[str] = set()
    answered_call_ids: set[str] = set()
    for r in rows:
        role = r["role"]
        if role == "user":
            content = r["content"]
            if r.get("images"):
                content = _parts_with_images(content, r["images"])
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            m = {"role": "assistant", "content": r["content"]}
            tcs = r.get("tool_calls")
            # Only replay well-formed OpenAI tool calls (id + function.name)
            if tcs and isinstance(tcs[0], dict) and tcs[0].get("id") and tcs[0].get("function"):
                m["tool_calls"] = tcs
                valid_call_ids.update(
                    tc.get("id", "") for tc in tcs if tc.get("id")
                )
            out.append(m)
        elif role == "tool":
            # Prefer the dedicated tool_call_id column; fall back to the
            # legacy convention of stashing the id in the tool_calls column.
            tc_id = r.get("tool_call_id")
            if not tc_id:
                meta = (r.get("tool_calls") or [{}])[0]
                tc_id = meta.get("id", "")
            if tc_id and tc_id not in valid_call_ids:
                continue  # orphaned tool result; skip to keep history valid
            answered_call_ids.add(tc_id)
            content = r["content"]
            if r.get("images"):
                # tool-attached image (view_image): replay as parts so a
                # vision model still sees it on resume
                try:
                    text = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    text = content
                if not isinstance(text, str):
                    text = json.dumps(text.get("note", "[image]"))
                content = _parts_with_images(text, r["images"])
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id or "",
                    "content": content,
                }
            )
        # system rows skipped; we inject our own system prompt fresh each turn
    # Tool calls that never got a result (e.g. the app closed while an
    # ask_user question was pending) must still be answered or most
    # OpenAI-compatible APIs reject the replayed history.
    for tc_id in valid_call_ids - answered_call_ids:
        out.append(
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": json.dumps(
                    {"answer": None, "note": "not answered (session ended)"}
                ),
            }
        )
    return out


async def run_agent(
    conversation_id: int,
    user_text: str,
    workspace: str,
    image_paths: list | None = None,
) -> AsyncIterator[str]:
    """Execute one user turn. Yields JSON-line event strings.

    image_paths: rel paths (under backend/data/images/) of images the user
    attached; already saved to disk by the API layer."""
    # Persist the user message first
    await add_message(
        conversation_id, "user", user_text, images=image_paths or None
    )

    # Per-conversation system prompt override (Q17) wins over the global one
    conv = await get_conversation(conversation_id)
    system_prompt = (conv or {}).get("system_prompt_override") or _default_system_prompt()

    # Full context each turn: system prompt + persisted history
    history = await load_history(conversation_id)
    messages = [{"role": "system", "content": system_prompt}] + history

    tools = get_schemas()
    cancel_ev = asyncio.Event()
    _cancel_events[conversation_id] = cancel_ev

    # Per-turn step budget; 0 or blank means unlimited (Stop button still ends
    # the turn). Configured in Settings → Max steps or config.json `max_steps`.
    try:
        max_steps = int(load_config().get("max_steps") or 0)
    except (TypeError, ValueError):
        max_steps = DEFAULT_MAX_STEPS

    try:
        for _step in range(max_steps) if max_steps > 0 else itertools.count():
            if cancel_ev.is_set():
                yield _ndjson({"type": "stopped", "reason": "cancelled by user"})
                return

            content_acc: list[str] = []
            tool_calls = None
            try:
                stream = await model_client.chat(messages, tools=tools, stream=True)
            except model_client.ModelError as e:
                # Auto-recovery (Q23): retry once with corrective context.
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"Your previous request failed with: {e}. "
                            "Retry with a corrected request."
                        ),
                    }
                )
                stream = await model_client.chat(messages, tools=tools, stream=True)

            async for ev in stream:
                if cancel_ev.is_set():
                    yield _ndjson({"type": "stopped", "reason": "cancelled by user"})
                    return
                if ev["type"] == "content":
                    content_acc.append(ev["text"])
                    yield _ndjson({"type": "text", "text": ev["text"]})
                elif ev["type"] == "tool_calls":
                    tool_calls = ev["tool_calls"]

            assistant_content = "".join(content_acc)

            # Persist assistant message (with tool calls if any)
            await add_message(
                conversation_id,
                "assistant",
                assistant_content,
                tool_calls=tool_calls,
            )

            # No tool calls => final answer; turn complete
            if not tool_calls:
                yield _ndjson({"type": "done"})
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": tool_calls,
                }
            )

            # Execute each requested tool call in order
            for tc in tool_calls:
                if cancel_ev.is_set():
                    yield _ndjson({"type": "stopped", "reason": "cancelled by user"})
                    return
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    result = {"error": f"Invalid JSON arguments: {e}"}
                else:
                    yield _ndjson(
                        {
                            "type": "tool_start",
                            "name": name,
                            "args": args,
                            "call_id": tc.get("id", ""),
                        }
                    )
                    if name == "ask_user":
                        result = await _ask_user(
                            conversation_id, tc.get("id", ""), args, cancel_ev
                        )
                    else:
                        result = await execute_tool(name, args, workspace)

                result_str = json.dumps(result)[:MAX_TOOL_RESULT_CHARS]
                # A tool that attached an image (view_image) becomes a
                # multimodal parts list for the live LLM call.
                image_rel = result.get("image") if isinstance(result, dict) else None
                tool_content = result_str
                image_data_url = load_data_url(image_rel) if image_rel else None
                if image_data_url:
                    tool_content = [
                        {"type": "text", "text": result_str},
                        _image_part(image_data_url),
                    ]
                yield _ndjson(
                    {
                        "type": "tool_result",
                        "name": name,
                        "result": result,
                        "image": image_rel,
                        "call_id": tc.get("id", ""),
                    }
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": tool_content,
                    }
                )
                # Persist tool result; store call id + name in tool_calls column
                await add_message(
                    conversation_id,
                    "tool",
                    result_str,
                    tool_calls=[{"id": tc.get("id", ""), "name": name}],
                    tool_call_id=tc.get("id", ""),
                    images=[image_rel] if image_rel else None,
                )

        yield _ndjson({
            "type": "error",
            "message": (
                f"Step budget ({max_steps}) exhausted — raise it in "
                "Settings → Max steps (or config.json `max_steps`; 0 = unlimited)"
            ),
        })

    except model_client.ModelError as e:
        yield _ndjson({"type": "error", "message": str(e)})
    except Exception as e:  # noqa: BLE001
        yield _ndjson({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        _cancel_events.pop(conversation_id, None)