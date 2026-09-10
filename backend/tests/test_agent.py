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


# ---------------------------------------------------------------- powershell

import os


@pytest.mark.skipif(os.name != "nt", reason="powershell.exe is Windows-only")
@pytest.mark.asyncio
async def test_powershell_tool(tmp_path):
    r = await execute_tool("powershell", {"command": "Write-Output hi-ps"}, str(tmp_path))
    assert r["exit_code"] == 0
    assert "hi-ps" in r["output"]


@pytest.mark.skipif(os.name != "nt", reason="powershell.exe is Windows-only")
@pytest.mark.asyncio
async def test_powershell_timeout(tmp_path):
    r = await execute_tool(
        "powershell",
        {"command": "Start-Sleep -Seconds 30", "timeout_seconds": 2},
        str(tmp_path),
    )
    assert r["timed_out"] is True


# ---------------------------------------------------------------- images

def test_image_roundtrip():
    from backend.agent.imagedata import IMAGES_ROOT, load_data_url, save_data_url

    # 1x1 transparent PNG
    data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    rel = save_data_url(data_url, subdir="test")
    assert rel is not None and rel.startswith("test/")
    assert (IMAGES_ROOT / rel).is_file()
    back = load_data_url(rel)
    assert back is not None and back.startswith("data:image/png;base64,")
    assert load_data_url("../escape.png") is None
    assert save_data_url("not a data url") is None


@pytest.mark.asyncio
async def test_db_images_roundtrip(tmp_path):
    from backend.db.database import add_message, create_conversation, get_messages

    cid = await create_conversation("img")
    await add_message(cid, "user", "see this", images=["1/a.png", "1/b.jpg"])
    rows = await get_messages(cid)
    assert rows[0]["images"] == ["1/a.png", "1/b.jpg"]
    # messages without images come back as an empty list
    await add_message(cid, "assistant", "ok")
    rows = await get_messages(cid)
    assert rows[1]["images"] == []


@pytest.mark.asyncio
async def test_history_rebuilds_image_parts(fake_model, tmp_path, monkeypatch):
    """A user message with images replays as a multimodal parts list."""
    from backend.db.database import add_message, create_conversation

    from backend.agent import imagedata

    data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    rel = imagedata.save_data_url(data_url, subdir="test")
    cid = await create_conversation("parts")
    await add_message(cid, "user", "look", images=[rel])

    captured = {}

    async def fake_chat(messages, tools=None, stream=True):
        captured["messages"] = messages
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    await collect(loop.run_agent(cid, "next", str(tmp_path)))

    user_msgs = [m for m in captured["messages"] if m["role"] == "user"]
    assert user_msgs[0]["content"][0] == {"type": "text", "text": "look"}
    assert user_msgs[0]["content"][1]["type"] == "image_url"
    # the missing-file case degrades to a text note instead of crashing
    await add_message(cid, "user", "gone", images=["test/does-not-exist.png"])
    await collect(loop.run_agent(cid, "next2", str(tmp_path)))
    user_msgs = [m for m in captured["messages"] if m["role"] == "user"]
    gone = next(m for m in user_msgs if isinstance(m["content"], list)
                and m["content"][0].get("text", "").startswith("gone"))
    assert any(
        isinstance(p, dict) and "missing" in p.get("text", "") for p in gone["content"]
    )


@pytest.mark.asyncio
async def test_view_image_tool_result_attaches_image(fake_model, tmp_path, monkeypatch):
    """A tool result carrying an image becomes a parts list in the live
    LLM messages and persists the image rel path."""
    from backend.db.database import create_conversation, get_messages

    from backend.agent import webtools

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d4944415478da63fccf00f6030003030100c9fe92"
        "ef0000000049454e44ae426082"
    )

    async def fake_view_image(url, workspace=None):
        from backend.agent.imagedata import save_bytes

        return {"image": save_bytes(png, "png", subdir="test"), "url": url,
                "note": "attached"}

    monkeypatch.setattr(webtools, "view_image", fake_view_image)
    # the dispatch table holds the function reference itself, so patch it there
    import backend.agent.tools as tools_mod
    monkeypatch.setitem(tools_mod.EXECUTORS, "view_image", fake_view_image)

    # script: model calls view_image, then answers
    call = {"id": "c1", "type": "function", "function": {
        "name": "view_image", "arguments": '{"url": "https://x/img.png"}'}}
    fake_model.append([{"type": "tool_calls", "tool_calls": [call]}])
    fake_model.append([{"type": "content", "text": "I see it"}, {"type": "finish"}])

    cid = await create_conversation("viewimg")
    events = await collect(loop.run_agent(cid, "look at https://x/img.png", str(tmp_path)))
    assert any(e["type"] == "tool_result" and e.get("image") for e in events)

    rows = await get_messages(cid)
    tool_rows = [r for r in rows if r["role"] == "tool"]
    assert tool_rows and tool_rows[0]["images"]


# ---------------------------------------------------------------- web tools (offline bits)

def test_strip_html():
    from backend.agent.webtools import strip_html

    html = "<head><style>x{}</style></head><p>Hello <b>world</b></p><p>Second</p>"
    text = strip_html(html)
    assert "Hello world" in text and "Second" in text
    assert "<" not in text


@pytest.mark.asyncio
async def test_web_search_ddg_post_fallback(monkeypatch):
    """When Chrome fails, the direct POST route still parses results."""
    from backend.agent import webtools

    ddg_html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">'
        'Example <b>Site</b></a>'
        '<a class="result__snippet" href="#">The snippet</a>'
    )

    async def fail_browser(url, timeout=30):
        raise RuntimeError("no chrome")

    monkeypatch.setattr(webtools, "_browser_get", fail_browser)
    monkeypatch.setattr(
        webtools, "_http_get",
        lambda url, timeout=15, data=None: ddg_html,
    )
    out = await webtools.web_search("query")
    assert "1. Example Site" in out
    assert "https://example.com" in out
    assert "The snippet" in out


@pytest.mark.asyncio
async def test_web_fetch_block_marker(monkeypatch):
    """A bot-wall page from Chrome falls back to direct HTTP."""
    from backend.agent import webtools

    async def chrome_wall(url, timeout=30):
        return "<html><body>Just a moment...</body></html>"

    monkeypatch.setattr(webtools, "_browser_get", chrome_wall)

    def direct(url, timeout=15, data=None):
        return "<html><title>Real</title><p>actual content here</p></html>"

    monkeypatch.setattr(webtools, "_http_get", direct)
    out = await webtools.web_fetch("https://example.com")
    assert "actual content" in out and "(via direct" in out


# ---------------------------------------------------------------- ask_user

@pytest.mark.asyncio
async def test_ask_user_answer_resumes_loop(fake_model, tmp_path):
    """The loop blocks at ask_user until resolve_answer delivers the answer."""
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("t3")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "q1",
                "type": "function",
                "function": {
                    "name": "ask_user",
                    "arguments": json.dumps({
                        "question": "Which framework?",
                        "options": [{"label": "React"}, {"label": "Svelte"}],
                    }),
                },
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    import asyncio
    agent = loop.run_agent(cid, "go", str(tmp_path))

    async def answer_when_asked():
        for _ in range(200):
            if loop.resolve_answer(cid, "q1", "React"):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("question never became pending")

    results = await asyncio.gather(collect(agent), answer_when_asked())
    events = results[0]
    types = [e["type"] for e in events]
    assert types == ["tool_start", "tool_result", "text", "done"]
    assert events[1]["name"] == "ask_user"
    assert events[1]["result"] == {"answer": "React"}

    msgs = await get_messages(cid)
    tool_row = [m for m in msgs if m["role"] == "tool"][0]
    assert json.loads(tool_row["content"]) == {"answer": "React"}


@pytest.mark.asyncio
async def test_ask_user_cancel_records_no_answer(fake_model, tmp_path):
    """Stopping the run while a question is pending records 'not answered'."""
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("t4")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "q2",
                "type": "function",
                "function": {
                    "name": "ask_user",
                    "arguments": json.dumps({"question": "?", "options": []}),
                },
            }],
        },
    ])

    import asyncio
    agent = loop.run_agent(cid, "go", str(tmp_path))

    async def cancel_when_asked():
        for _ in range(200):
            key = f"{cid}:q2"
            if key in loop._pending_answers:
                loop.cancel_agent(cid)
                return
            await asyncio.sleep(0.01)
        raise AssertionError("question never became pending")

    results = await asyncio.gather(collect(agent), cancel_when_asked())
    events = results[0]
    result = next(e for e in events if e["type"] == "tool_result")["result"]
    assert result["answer"] is None
    assert "did not answer" in result["note"]
    # and the turn ends as stopped, with the tool result persisted
    assert events[-1]["type"] == "stopped"
    msgs = await get_messages(cid)
    tool_row = [m for m in msgs if m["role"] == "tool"][0]
    assert json.loads(tool_row["content"])["answer"] is None


@pytest.mark.asyncio
async def test_load_history_fills_unanswered_tool_call(fake_model, tmp_path):
    """A tool call with no persisted result (crash mid-question) gets a
    synthetic 'not answered' tool message on replay."""
    from backend.db.database import add_message, create_conversation

    cid = await create_conversation("t5")
    await add_message(cid, "user", "go")
    await add_message(
        cid, "assistant", "",
        tool_calls=[{"id": "qx", "type": "function",
                     "function": {"name": "ask_user", "arguments": "{}"}}],
    )
    history = await loop.load_history(cid)
    tool_msgs = [m for m in history if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "qx"
    assert "not answered" in tool_msgs[0]["content"]
