"""Tests for the agent loop and tools (model is faked)."""
import json

import pytest

from backend.agent import loop
from backend.agent.tools import execute_tool, resolve_path


# ---------------------------------------------------------------- tools

@pytest.mark.asyncio
async def test_bash_tool(tmp_path):
    r = await execute_tool("bash", {"command": "echo hi"}, str(tmp_path))
    assert r["exit_code"] == 0
    assert "hi" in r["output"]


@pytest.mark.asyncio
async def test_write_read_edit_roundtrip(tmp_path):
    ws = str(tmp_path)
    await execute_tool("write_file", {"path": "a.txt", "content": "hello world"}, ws)
    r = await execute_tool("read_file", {"path": "a.txt"}, ws)
    assert "hello world" in r["content"]
    r = await execute_tool("edit_file", {"path": "a.txt", "old_text": "world", "new_text": "there"}, ws)
    assert r["replaced"] == 1
    r = await execute_tool("read_file", {"path": "a.txt"}, ws)
    assert "hello there" in r["content"]


@pytest.mark.asyncio
async def test_edit_requires_unique_match(tmp_path):
    ws = str(tmp_path)
    await execute_tool("write_file", {"path": "b.txt", "content": "x x x"}, ws)
    r = await execute_tool("edit_file", {"path": "b.txt", "old_text": "x", "new_text": "y"}, ws)
    assert "matches 3" in r["error"]


def test_path_escape_blocked(tmp_path):
    with pytest.raises(ValueError):
        resolve_path(str(tmp_path), "../outside.txt")


# ---------------------------------------------------------------- loop

class FakeStream:
    """Async iterator over preset events."""

    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


@pytest.fixture
def fake_model(monkeypatch):
    """Patch model_client.chat to return scripted responses per call."""
    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


async def collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


@pytest.mark.asyncio
async def test_agent_direct_answer(fake_model, tmp_path):
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("t")
    fake_model.append([{"type": "content", "text": "Hello!"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    types = [e["type"] for e in events]
    assert types == ["text", "done"]

    msgs = await get_messages(cid)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[1]["content"] == "Hello!"


@pytest.mark.asyncio
async def test_agent_tool_cycle(fake_model, tmp_path):
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("t2")
    # Step 1: model calls bash; Step 2: final answer
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo tool ran"})},
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "All done"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    types = [e["type"] for e in events]
    # tool_start/tool_result, then the final step's text delta, then done
    assert types == ["tool_start", "tool_result", "text", "done"]
    assert events[1]["result"]["exit_code"] == 0
    assert "tool ran" in events[1]["result"]["output"]

    msgs = await get_messages(cid)
    roles = [m["role"] for m in msgs]
    # user, assistant(tool_call), tool, assistant(final)
    assert roles == ["user", "assistant", "tool", "assistant"]
