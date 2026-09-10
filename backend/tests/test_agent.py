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


# ---------------------------------------------------------------- new tools

@pytest.mark.asyncio
async def test_create_file_refuses_overwrite(tmp_path):
    ws = str(tmp_path)
    r = await execute_tool("create_file", {"path": "n.txt", "content": "abc"}, ws)
    assert "bytes_written" in r
    r = await execute_tool("create_file", {"path": "n.txt", "content": "xyz"}, ws)
    assert "already exists" in r["error"]


@pytest.mark.asyncio
async def test_move_and_delete_file(tmp_path):
    ws = str(tmp_path)
    await execute_tool("write_file", {"path": "old.txt", "content": "data"}, ws)
    r = await execute_tool("move_file", {"src": "old.txt", "dst": "sub/new.txt"}, ws)
    assert r.get("moved") is True
    assert (tmp_path / "sub" / "new.txt").read_text() == "data"
    r = await execute_tool("delete_file", {"path": "sub/new.txt"}, ws)
    assert r.get("deleted") is True
    assert not (tmp_path / "sub" / "new.txt").exists()


@pytest.mark.asyncio
async def test_search_files_content_and_glob(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "a.py").write_text("def hello():\n    pass\n")
    (tmp_path / "b.txt").write_text("hello world\n")

    r = await execute_tool("search_files", {"pattern": "hello", "glob": "*.py"}, ws)
    assert r["count"] == 1
    assert r["matches"][0]["path"] == "a.py"

    r = await execute_tool("search_files", {"glob": "*.txt"}, ws)
    assert [m["path"] for m in r["matches"]] == ["b.txt"]


@pytest.mark.asyncio
async def test_read_file_line_range(tmp_path):
    ws = str(tmp_path)
    await execute_tool("write_file", {"path": "big.txt", "content": "\n".join(f"l{i}" for i in range(1, 101))}, ws)
    r = await execute_tool("read_file", {"path": "big.txt", "start_line": 10, "end_line": 12}, ws)
    assert r["start_line"] == 10
    assert "l10" in r["content"] and "l13" not in r["content"]
    assert r["truncated"] is True


@pytest.mark.asyncio
async def test_git_tools(tmp_path):
    ws = str(tmp_path)
    await execute_tool("bash", {"command": "git init -q && git config user.email t@t && git config user.name t"}, ws)
    await execute_tool("write_file", {"path": "f.txt", "content": "v1"}, ws)
    r = await execute_tool("git_status", {}, ws)
    assert "f.txt" in r["output"]
    await execute_tool("git_add", {}, ws)
    r = await execute_tool("git_commit", {"message": "first"}, ws)
    assert r["exit_code"] == 0
    r = await execute_tool("git_status", {}, ws)
    assert "f.txt" not in r["output"]  # clean tree


@pytest.mark.asyncio
async def test_search_escape_blocked(tmp_path):
    r = await execute_tool("write_file", {"path": "../evil.txt", "content": "x"}, str(tmp_path))
    assert "error" in r


# ---------------------------------------------------------------- loop extras

@pytest.mark.asyncio
async def test_system_prompt_override(fake_model, tmp_path, monkeypatch):
    from backend.db.database import create_conversation, update_conversation

    cid = await create_conversation("t3")
    await update_conversation(cid, system_prompt_override="You are a pirate.")
    captured = {}

    async def fake_chat(messages, tools=None, stream=True):
        captured["system"] = messages[0]["content"]
        return FakeStream([{"type": "content", "text": "arr"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    assert captured["system"] == "You are a pirate."


@pytest.mark.asyncio
async def test_agent_cancel(fake_model, tmp_path, monkeypatch):
    from backend.db.database import create_conversation

    cid = await create_conversation("t4")
    loop.cancel_agent(cid)  # cancel before start: nothing running, no-op event
    fake_model.append([{"type": "content", "text": "x"}, {"type": "finish"}])

    # register cancel after the first event by scripting via monkeypatch wrapper
    async def fake_chat(messages, tools=None, stream=True):
        loop.cancel_agent(cid)
        return FakeStream([{"type": "content", "text": "should be cut off"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    # The stream starts before cancel takes effect, but the loop must stop
    # before emitting 'done'
    assert events[-1]["type"] == "stopped"
