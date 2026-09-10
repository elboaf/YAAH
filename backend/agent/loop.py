"""The agent loop: model <-> tools cycle with streaming events.

Runs until the model produces a final answer or the step budget is exhausted.
Emits JSON-line events for the frontend:
  {'type': 'text', 'text': ...}             - assistant text delta
  {'type': 'tool_start', 'name', 'args'}    - tool execution beginning
  {'type': 'tool_result', 'name', 'result'} - tool output
  {'type': 'done'}                          - final answer complete
  {'type': 'error', 'message'}              - fatal error
"""
import json
from typing import AsyncIterator

from backend.agent import model_client
from backend.agent.tools import execute_tool, get_schemas
from backend.db.database import add_message, get_messages

MAX_STEPS = 25
MAX_TOOL_RESULT_CHARS = 20_000

SYSTEM_PROMPT = """You are an expert AI coding agent working inside a user's project workspace.

You have tools: bash (shell commands), read_file, write_file, edit_file.

Guidelines:
- Explore before acting: read files and run discovery commands before editing.
- Prefer edit_file for targeted changes; write_file only for new files or full rewrites.
- Verify your work: run tests/builds after changes when possible.
- Be concise in prose; let tools do the talking.
- Paths are relative to the workspace root."""


def _sse(event: dict) -> str:
    return json.dumps(event) + "\n"


async def load_history(conversation_id: int) -> list:
    """Load persisted messages back into OpenAI chat format."""
    rows = await get_messages(conversation_id)
    out = []
    for r in rows:
        role = r["role"]
        if role == "user":
            out.append({"role": "user", "content": r["content"]})
        elif role == "assistant":
            m = {"role": "assistant", "content": r["content"]}
            tcs = r.get("tool_calls")
            # Only replay well-formed OpenAI tool calls (id + function.name)
            if tcs and isinstance(tcs[0], dict) and tcs[0].get("id") and tcs[0].get("function"):
                m["tool_calls"] = tcs
            out.append(m)
        elif role == "tool":
            meta = (r.get("tool_calls") or [{}])[0]
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": meta.get("id", ""),
                    "content": r["content"],
                }
            )
        # system rows skipped; we inject our own system prompt fresh each turn
    return out


async def run_agent(
    conversation_id: int,
    user_text: str,
    workspace: str,
) -> AsyncIterator[str]:
    """Execute one user turn. Yields JSON-line event strings."""
    # Persist the user message first
    await add_message(conversation_id, "user", user_text)

    # Full context each turn: system prompt + persisted history
    history = await load_history(conversation_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    tools = get_schemas()

    try:
        for _step in range(MAX_STEPS):
            content_acc: list[str] = []
            tool_calls = None
            stream = await model_client.chat(messages, tools=tools, stream=True)

            async for ev in stream:
                if ev["type"] == "content":
                    content_acc.append(ev["text"])
                    yield _sse({"type": "text", "text": ev["text"]})
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
                yield _sse({"type": "done"})
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
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    result = {"error": f"Invalid JSON arguments: {e}"}
                else:
                    yield _sse({"type": "tool_start", "name": name, "args": args})
                    result = await execute_tool(name, args, workspace)

                result_str = json.dumps(result)[:MAX_TOOL_RESULT_CHARS]
                yield _sse({"type": "tool_result", "name": name, "result": result})

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result_str,
                    }
                )
                # Persist tool result; store call id + name in tool_calls column
                await add_message(
                    conversation_id,
                    "tool",
                    result_str,
                    tool_calls=[{"id": tc.get("id", ""), "name": name}],
                )

        yield _sse({"type": "error", "message": f"Step budget ({MAX_STEPS}) exhausted"})

    except model_client.ModelError as e:
        yield _sse({"type": "error", "message": str(e)})
    except Exception as e:  # noqa: BLE001
        yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})