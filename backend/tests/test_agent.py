"""Tests for the agent loop and tools (model is faked)."""
import asyncio
import json

import pytest

from backend.agent import loop
from backend.agent.tools import execute_tool, resolve_path, workspace_root
from backend.tests.gitutil import run_git

from pathlib import Path


# ---------------------------------------------------------------- tools

@pytest.mark.asyncio
async def test_bash_tool(tmp_path):
    r = await execute_tool("bash", {"command": "echo hi"}, str(tmp_path))
    assert r["exit_code"] == 0
    assert "hi" in r["output"]


@pytest.mark.asyncio
async def test_bash_streams_chunks_live(tmp_path):
    """on_chunk must fire incrementally while the command runs, before the
    final result comes back — that is the whole point of tool_progress."""
    chunks: list[str] = []
    r = await execute_tool(
        "bash",
        {"command": "echo one; echo two", "timeout_seconds": 10},
        str(tmp_path),
        on_chunk=chunks.append,
    )
    assert r["exit_code"] == 0
    assert chunks, "no live chunks delivered"
    assert "one" in "".join(chunks)
    assert "one" in r["output"] and "two" in r["output"]
    # Final output must be the full stream, not just the first chunk.
    assert "".join(chunks).strip() == r["output"].strip()


@pytest.mark.asyncio
async def test_bash_stream_timeout_kills_tree(tmp_path):
    """The incremental read loop must enforce the deadline and kill the
    tree just like the old communicate() path did."""
    import sys

    if sys.platform == "win32":
        command = 'start /b ping -n 30 127.0.0.1 >nul'
    else:
        command = "sleep 30 &"
    chunks: list[str] = []
    r = await asyncio.wait_for(
        execute_tool(
            "bash",
            {"command": command, "timeout_seconds": 2},
            str(tmp_path),
            on_chunk=chunks.append,
        ),
        timeout=15,
    )
    assert r["timed_out"] is True


@pytest.mark.asyncio
async def test_bash_on_chunk_errors_swallowed(tmp_path):
    """A throwing on_chunk (dead UI stream) must never fail the tool."""
    def bad(_chunk):
        raise RuntimeError("stream gone")

    r = await execute_tool(
        "bash", {"command": "echo ok"}, str(tmp_path), on_chunk=bad
    )
    assert r["exit_code"] == 0
    assert "ok" in r["output"]


@pytest.mark.asyncio
async def test_bash_timeout_kills_backgrounded_child(tmp_path):
    """A backgrounded child inherits the output pipe; the timeout path must
    kill the whole tree or run_bash hangs in the post-kill communicate()
    (and leaks an orphan server)."""
    import sys

    if sys.platform == "win32":
        command = 'start /b ping -n 30 127.0.0.1 >nul'
    else:
        command = "sleep 30 &"
    r = await asyncio.wait_for(
        execute_tool("bash", {"command": command, "timeout_seconds": 2}, str(tmp_path)),
        timeout=15,
    )
    assert r["timed_out"] is True


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


def test_default_workspace_is_home():
    from pathlib import Path

    for ws in ("", ".", None):
        assert workspace_root(ws) == Path.home().resolve()
    # File tools resolve against home and still block escapes outside it.
    assert str(resolve_path("", "notes.txt")).startswith(str(Path.home().resolve()))
    with pytest.raises(ValueError):
        resolve_path("", "../outside.txt")


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
async def test_run_emits_persisted_per_file_change_summary(fake_model, tmp_path, monkeypatch):
    from backend.db.database import create_conversation, get_messages

    # Use a non-Git workspace to exercise the filesystem-snapshot fallback;
    # it already contains earlier-run dirt that must not be reported again.
    wt = tmp_path / "workspace"
    wt.mkdir()
    (wt / "existing.txt").write_text("before\n", encoding="utf-8")
    (wt / "previous-run.txt").write_text("older work\n", encoding="utf-8")
    (wt / "earlier.txt").write_text("from prior turn\n", encoding="utf-8")
    baseline = await loop.file_changes.snapshot_workspace(str(wt))
    original_snapshot = loop.file_changes.snapshot_workspace
    snapshots = 0

    async def fake_snapshot(path):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 1:
            return baseline
        # Simulate the run changing one existing file, adding one, and
        # deleting one. The earlier file remains untouched.
        (wt / "existing.txt").write_text("after\nnew line\n", encoding="utf-8")
        (wt / "added.txt").write_text("added line\n", encoding="utf-8")
        (wt / "previous-run.txt").unlink()
        return await original_snapshot(path)

    async def no_title(*_args):
        return None

    monkeypatch.setattr(loop, "_generate_conversation_title", no_title)
    monkeypatch.setattr(loop.file_changes, "snapshot_workspace", fake_snapshot)
    cid = await create_conversation("file change summary")
    fake_model.append([{"type": "content", "text": "Done."}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(wt)))
    event = next(e for e in events if e["type"] == "file_changes")
    assert event["added"] == 3
    assert event["deleted"] == 2
    assert {f["path"] for f in event["files"]} == {"existing.txt", "added.txt", "previous-run.txt"}
    assert "earlier.txt" not in {f["path"] for f in event["files"]}
    assert events[-1]["type"] == "done"

    persisted = await get_messages(cid)
    saved = next(json.loads(m["content"]) for m in persisted if m["role"] == "system" and "file_changes" in m["content"])
    assert saved["file_changes"] == {"files": event["files"], "added": 3, "deleted": 2}


@pytest.mark.asyncio
async def test_git_tool_run_emits_and_persists_separate_git_summary(fake_model, tmp_path, monkeypatch):
    from backend.db.database import create_conversation, get_messages
    from backend.agent import worktrees

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    (repo / "file.txt").write_text("before\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "baseline")
    wt = await worktrees.ensure_isolated(str(repo), chat_id="git-summary-test")
    (Path(wt) / "file.txt").write_text("after\n", encoding="utf-8")
    run_git(Path(wt), "add", "-A")
    cid = await create_conversation("git activity summary")
    monkeypatch.setattr(loop, "_generate_conversation_title", lambda *_: asyncio.sleep(0, result=None))
    fake_model.append([{
        "type": "tool_calls",
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "git_commit", "arguments": json.dumps({"message": "Run summary commit"})},
        }],
    }])
    fake_model.append([{"type": "content", "text": "Committed."}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "commit this", str(wt)))
    summary_event = next(event for event in events if event["type"] == "git_activity")
    lane = next(lane for lane in summary_event["lanes"] if lane["id"] == "parent")
    assert lane["branch_action"] == "reused"
    assert lane["operations"][-1]["operation"] == "commit"
    assert lane["operations"][-1]["subject"] == "Run summary commit"
    saved = await get_messages(cid)
    row = next(json.loads(message["content"]) for message in saved if message["role"] == "system" and "git_activity" in message["content"])
    assert row["git_activity"]["run_id"] == summary_event["run_id"]


@pytest.mark.asyncio
async def test_usage_event_uses_frontend_context_token_field(fake_model, tmp_path):
    """The streamed usage count must match the frontend's usage_tokens contract."""
    from backend.db.database import create_conversation

    cid = await create_conversation("usage-event-contract")
    fake_model.append([
        {"type": "content", "text": "A real answer."},
        {"type": "usage", "usage": {"prompt_tokens": 1234}},
        {"type": "finish"},
    ])

    events = await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    usage = next(event for event in events if event["type"] == "usage")

    assert usage["usage_tokens"] == 1234


@pytest.mark.asyncio
@pytest.mark.parametrize("split", range(1, 6))
async def test_agent_say_opener_split_never_streams_tag(fake_model, tmp_path, split):
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation(f"say-split-{split}")
    opener = "<say>"
    fake_model.append([
        {"type": "content", "text": f"Visible answer. {opener[:split]}"},
        {"type": "content", "text": f"{opener[split:]}private briefing</say> after"},
        {"type": "finish"},
    ])

    events = await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    visible = "".join(e["text"] for e in events if e["type"] == "text")
    assert visible == "Visible answer.  after"
    assert "<say" not in visible
    messages = await get_messages(cid)
    assert "<say" not in messages[-1]["content"]
    assert messages[-1]["content"] == "Visible answer.  after"


@pytest.mark.asyncio
@pytest.mark.parametrize("split", range(1, 7))
async def test_agent_say_closer_split_never_streams_tag(fake_model, tmp_path, split):
    from backend.db.database import create_conversation

    cid = await create_conversation(f"say-close-{split}")
    closer = "</say>"
    fake_model.append([
        {"type": "content", "text": "Visible <say>private briefing" + closer[:split]},
        {"type": "content", "text": closer[split:] + " tail"},
        {"type": "finish"},
    ])

    events = await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    visible = "".join(e["text"] for e in events if e["type"] == "text")
    assert visible == "Visible  tail"
    assert "<say" not in visible


@pytest.mark.asyncio
async def test_agent_unclosed_say_tag_not_streamed_or_persisted(fake_model, tmp_path):
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("say-unclosed")
    fake_model.append([
        {"type": "content", "text": "Visible answer. <say>truncated briefing"},
        {"type": "finish"},
    ])

    events = await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    visible = "".join(e["text"] for e in events if e["type"] == "text")
    messages = await get_messages(cid)
    assert visible == "Visible answer. "
    assert messages[-1]["content"] == "Visible answer."
    assert "<say" not in messages[-1]["content"]


@pytest.mark.asyncio
async def test_agent_direct_answer(fake_model, tmp_path):
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("t")
    fake_model.append([{"type": "content", "text": "Hello!"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    types = [e["type"] for e in events]
    assert types == ["text", "say", "done"]

    msgs = await get_messages(cid)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[1]["content"] == "Hello!"


# ------------------------------------------------- auto titles (issue #60)

@pytest.mark.asyncio
async def test_first_turn_generates_model_title(fake_model, tmp_path, monkeypatch):
    """After the first turn of a fresh chat the mechanical title slice is
    replaced by a model-generated one, and a 'title' event is emitted."""
    from backend.db.database import create_conversation, get_conversation

    cid = await create_conversation("help me fix the flaky test suite")  # <40 chars: exact slice
    # Title call = the non-stream chat; the turn call stays on the fixture's
    # scripted stream. Route by the `stream` flag the two paths differ in.
    async def fake_title_chat(messages, tools=None, stream=False):
        assert stream is False
        assert any("title" in (m.get("content") or "") for m in messages)
        return {"choices": [{"message": {"content": '"Flaky Test Suite Triage"'}}]}

    real_chat = loop.model_client.chat

    async def routed(messages, tools=None, stream=True):
        if stream:
            return await real_chat(messages, tools=tools, stream=True)
        return await fake_title_chat(messages, tools=tools, stream=False)

    monkeypatch.setattr(loop.model_client, "chat", routed)

    fake_model.append([{"type": "content", "text": "On it."}, {"type": "finish"}])
    events = await collect(loop.run_agent(cid, "help me fix the flaky test suite", str(tmp_path)))
    title_events = [e for e in events if e["type"] == "title"]
    assert title_events and title_events[0]["title"] == "Flaky Test Suite Triage"
    conv = await get_conversation(cid)
    assert conv["title"] == "Flaky Test Suite Triage"


@pytest.mark.asyncio
async def test_title_skips_renamed_and_agent_chats(fake_model, tmp_path, monkeypatch):
    """A manually renamed (or agent-pinned) title is never overwritten — no
    title event, no model call for the title."""
    from backend.db.database import create_conversation

    cid = await create_conversation("my custom name")
    calls = []

    async def routed(messages, tools=None, stream=True):
        if not stream:
            calls.append("title-call")
            return {"choices": [{"message": {"content": "should not happen"}}]}
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", routed)
    events = await collect(loop.run_agent(cid, "hello there agent", str(tmp_path)))
    assert not [e for e in events if e["type"] == "title"]
    assert calls == []

    # Agent chats are explicitly excluded even when their title happens to
    # match the mechanical prompt slice.
    cid2 = await create_conversation("agent prompt", chat_type="agent")
    events2 = await collect(loop.run_agent(cid2, "agent prompt", str(tmp_path)))
    assert not [e for e in events2 if e["type"] == "title"]
    assert calls == []


@pytest.mark.asyncio
async def test_title_failure_keeps_slice_and_retries_next_turn(fake_model, tmp_path, monkeypatch):
    """A failed title call is silent (slice stays) and the next turn retries."""
    from backend.db.database import create_conversation, get_conversation

    cid = await create_conversation("debug crash on startup")
    state = {"calls": 0}

    async def routed(messages, tools=None, stream=True):
        if not stream:
            state["calls"] += 1
            raise RuntimeError("provider down")
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", routed)
    for prompt in ("debug crash on startup", "I have another detail"):
        events = await collect(loop.run_agent(cid, prompt, str(tmp_path)))
        assert not [e for e in events if e["type"] == "title"]
    conv = await get_conversation(cid)
    assert conv["title"] == "debug crash on startup"  # slice intact
    assert state["calls"] == 2  # retried against the original user message title


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
    # tool_start(/tool_progress…) then tool_result, final text delta, done.
    # tool_progress chunks are filtered: fast commands emit a variable number.
    core = [t for t in types if t != "tool_progress"]
    assert core == ["tool_start", "tool_result", "text", "say", "done"]
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["result"]["exit_code"] == 0
    assert "tool ran" in tr["result"]["output"]

    msgs = await get_messages(cid)
    roles = [m["role"] for m in msgs]
    # user, assistant(tool_call), tool, assistant(final)
    assert roles == ["user", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_agent_yields_tool_progress(fake_model, tmp_path):
    """A bash call now streams tool_progress events between tool_start and
    tool_result so the UI can show live output."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t-progress")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo streaming out"})},
            }],
        },
    ])
    fake_model.append([{"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    types = [e["type"] for e in events]
    assert types[0] == "tool_start"
    assert types[-1] == "done"
    progress = [e for e in events if e["type"] == "tool_progress"]
    assert progress, f"no tool_progress in {types}"
    assert all(e["call_id"] == "c1" for e in progress)
    assert "streaming out" in "".join(e["chunk"] for e in progress)
    # tool_result still lands after the last progress chunk
    assert types.index("tool_progress") < types.index("tool_result") < types.index("done")


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


# ---------------------------------------------------------------- concurrency

@pytest.mark.asyncio
async def test_cancel_during_setup_releases_conversation(fake_model, tmp_path, monkeypatch):
    """Aborting a stream while setup is suspended must release its run slot."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t-cancel-setup")
    entered = asyncio.Event()

    async def blocked_add_message(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(loop, "add_message", blocked_add_message)
    task = asyncio.create_task(anext(loop.run_agent(cid, "go", str(tmp_path))))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not loop.agent_is_running(cid), "cancelled setup leaked conversation run slot"


@pytest.mark.asyncio
async def test_second_run_same_conversation_rejected(fake_model, tmp_path):
    """While a turn is in flight, a second run on the SAME conversation must
    be refused (error event, no 'done') — two interleaved streams would
    clobber each other's cancel event and message ordering."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t-concurrent-same")
    gate = asyncio.Event()

    async def gated_chat(messages, tools=None, stream=True):
        await gate.wait()  # hold the first run open until we release it
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    original = loop.model_client.chat
    loop.model_client.chat = gated_chat
    try:
        first = asyncio.create_task(collect(loop.run_agent(cid, "go", str(tmp_path))))
        await asyncio.sleep(0.05)  # let the first run register + reach the model
        assert loop.agent_is_running(cid)
        second = await collect(loop.run_agent(cid, "again", str(tmp_path)))
        assert second[-1]["type"] == "error"
        assert "already running" in second[-1]["message"]
        gate.set()
        first_events = await first
        assert first_events[-1]["type"] == "done"
    finally:
        loop.model_client.chat = original
    assert not loop.agent_is_running(cid), "run must unregister in finally"


@pytest.mark.asyncio
async def test_two_conversations_run_concurrently(fake_model, tmp_path):
    """Different conversations must not block each other: both turns
    complete, each in its own stream."""
    from backend.db.database import create_conversation

    cids = [
        await create_conversation("t-concurrent-a"),
        await create_conversation("t-concurrent-b"),
    ]
    started = asyncio.Event()

    async def slow_chat(messages, tools=None, stream=True):
        if not started.is_set():
            started.set()
            await asyncio.sleep(0.1)  # first call stalls; second must proceed
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    original = loop.model_client.chat
    loop.model_client.chat = slow_chat
    try:
        results = await asyncio.gather(
            collect(loop.run_agent(cids[0], "a", str(tmp_path))),
            collect(loop.run_agent(cids[1], "b", str(tmp_path))),
        )
    finally:
        loop.model_client.chat = original
    for events in results:
        assert events[-1]["type"] == "done"
    for cid in cids:
        assert not loop.agent_is_running(cid)


@pytest.mark.asyncio
async def test_try_begin_run_claim_semantics():
    """try_begin_run claims once; the duplicate claim fails; discard resets."""
    cid = 987654
    loop._running_convs.discard(cid)  # isolation from other tests
    try:
        assert loop.try_begin_run(cid) is True
        assert loop.agent_is_running(cid)
        assert loop.try_begin_run(cid) is False
        loop._running_convs.discard(cid)  # what run_agent's finally does
        assert loop.try_begin_run(cid) is True
    finally:
        loop._running_convs.discard(cid)


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
        webtools, "_curl_get",
        lambda url, timeout=15, data=None: (_ for _ in ()).throw(
            RuntimeError("no curl_cffi")),
    )
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
    monkeypatch.setattr(
        webtools, "_curl_get",
        lambda url, timeout=15, data=None: (_ for _ in ()).throw(
            RuntimeError("no curl_cffi")),
    )

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
    assert types == ["tool_start", "tool_result", "text", "say", "done"]
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


@pytest.mark.asyncio
async def test_conversation_history_search_includes_compacted_and_auxiliary_text():
    from backend.db.database import (
        add_message, compact_conversation, create_conversation, get_messages,
        search_conversation_history,
    )

    cid = await create_conversation("history-search")
    first_id = await add_message(cid, "user", "remember amber-orbit")
    await add_message(
        cid, "assistant", "tool call",
        tool_calls=[{"id": "tc", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"target": "amber-orbit"})}}],
    )
    await add_message(cid, "tool", "tool result mentions AMBER-ORBIT", tool_call_id="tc")
    await add_message(cid, "assistant", "subagent result", sub_agent_transcript={"content": "amber-orbit detail"})
    rows = await get_messages(cid)
    assert await compact_conversation(cid, "summary mentions amber-orbit", rows[1]["id"]) == 2

    result = await search_conversation_history(cid, "AMBER-ORBIT", max_results=10)
    assert result["count"] >= 4
    assert any(m["message_id"] == first_id for m in result["matches"])
    assert any(m["field"] == "tool_calls" for m in result["matches"])
    assert any(m["field"] == "sub_agent_transcript" for m in result["matches"])
    assert any(m["role"] == "prompt_summary" for m in result["matches"])
    assert all("excerpt" in m for m in result["matches"])


@pytest.mark.asyncio
async def test_model_can_search_active_conversation_history(fake_model, tmp_path):
    from backend.db.database import add_message, create_conversation

    cid = await create_conversation("history-tool-loop")
    await add_message(cid, "user", "origin value cobalt-signal")
    fake_model.append([{
        "type": "tool_calls",
        "tool_calls": [{
            "id": "history-search-call",
            "type": "function",
            "function": {
                "name": "search_conversation_history",
                "arguments": json.dumps({"query": "cobalt-signal"}),
            },
        }],
    }])
    fake_model.append([{"type": "content", "text": "found it"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "look up the earlier value", str(tmp_path)))
    result = next(e["result"] for e in events if e["type"] == "tool_result")
    assert result["count"] >= 1
    assert any(m["role"] == "user" for m in result["matches"])


@pytest.mark.asyncio
async def test_search_conversation_history_executor_requires_active_conversation(tmp_path):
    from backend.agent.tools import execute_tool
    from backend.db.database import add_message, create_conversation

    unavailable = await execute_tool(
        "search_conversation_history", {"query": "secret"}, str(tmp_path)
    )
    assert "error" in unavailable
    cid = await create_conversation("search-executor")
    await add_message(cid, "user", "find me")
    found = await execute_tool(
        "search_conversation_history", {"query": "find me"}, str(tmp_path),
        conversation_id=cid,
    )
    assert found["count"] == 1


# ---------------------------------------------------------------- harness guidance
# Regression tests for the stalled FlyGD-Wingman verification run: a full
# pytest that outlives the tool cap, a cmd shell the prompt never named,
# and project notes the agent had no way to read.


@pytest.mark.asyncio
async def test_bash_timeout_clamp_is_signalled(tmp_path):
    """A request above the cap is clamped, and the model must be TOLD:
    a silent clamp reads like a hung run and invites endless retries."""
    from backend.agent.tools import MAX_BASH_TIMEOUT

    r = await execute_tool(
        "bash", {"command": "echo hi", "timeout_seconds": MAX_BASH_TIMEOUT + 1},
        str(tmp_path),
    )
    assert r["exit_code"] == 0
    assert "note" in r
    assert str(MAX_BASH_TIMEOUT) in r["note"]
    # At or under the cap: no note.
    r = await execute_tool("bash", {"command": "echo hi"}, str(tmp_path))
    assert "note" not in r


def test_env_line_names_real_shell(monkeypatch):
    import os

    from backend.agent.loop import _local_env_line, _shell_phrase

    # Pin the platform, not just the env vars: this test must take the
    # cmd branch even on the Linux CI runner.
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    line = _local_env_line()
    assert "cmd.exe" in line
    assert "findstr" in line  # the POSIX-tools caveat
    monkeypatch.setenv("COMSPEC", r"C:\Program Files\PowerShell\7\pwsh.exe")
    assert "pwsh.exe" in _local_env_line()
    assert "findstr" not in _local_env_line()
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert "zsh" in _shell_phrase(False)


def test_remote_env_line_windows_caveat():
    from backend.agent.remote import CMD_TOOLS_NOTE, RemoteSession

    win = RemoteSession("http://h", "p", {"windows": True, "os": "Windows"})
    assert CMD_TOOLS_NOTE in win.env_line()
    nix = RemoteSession("http://h", "p", {"windows": False, "os": "Linux"})
    assert "findstr" not in nix.env_line()


def test_agents_notes_injection(tmp_path):
    ws = str(tmp_path)
    assert loop._agents_notes(ws) == ""  # missing file -> no section
    (tmp_path / "AGENTS.md").write_text(
        "test_setup_catalog failures are pre-existing on this box", encoding="utf-8"
    )
    notes = loop._agents_notes(ws)
    assert "Project notes" in notes
    assert "pre-existing" in notes
    # Oversized file is clipped, with a marker, never an error.
    (tmp_path / "AGENTS.md").write_text("x" * (loop.MAX_AGENTS_NOTES_CHARS + 500), encoding="utf-8")
    clipped = loop._agents_notes(ws)
    assert len(clipped) < loop.MAX_AGENTS_NOTES_CHARS + 300
    assert "truncated" in clipped


@pytest.mark.asyncio
async def test_run_agent_injects_agents_notes(fake_model, tmp_path):
    from backend.db.database import create_conversation

    (tmp_path / "AGENTS.md").write_text("# Baseline\nknow-fails: 3", encoding="utf-8")
    cid = await create_conversation("t-notes")
    captured = {}

    async def fake_chat(messages, tools=None, stream=True):
        captured["system"] = messages[0]["content"]
        return FakeStream([{"type": "finish"}])

    import backend.agent.model_client as mc

    # run_agent's fake_model fixture patches loop.model_client.chat already;
    # capture through the same patch by wrapping.
    orig = loop.model_client.chat

    async def spy(messages, tools=None, stream=True):
        captured["system"] = messages[0]["content"]
        return await orig(messages, tools=tools, stream=stream)

    loop.model_client.chat = spy
    try:
        await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    finally:
        loop.model_client.chat = orig
    assert "know-fails: 3" in captured["system"]


async def test_chat_provider_model_override(monkeypatch):
    """A 'provider::model' per-agent override routes the call at that
    provider (base + key), not the active one; a bare id keeps the active
    provider and only swaps the model name."""
    import backend.agent.model_client as mc

    monkeypatch.setattr(mc, "load_config", lambda: {
        "providers": {
            "openrouter": {"api_base": "https://openrouter.ai/api/v1",
                           "api_key": "k-or", "model": "z-ai/glm-5.3-flash"},
            "llamacpp": {"api_base": "http://192.168.1.170:8080/v1",
                         "api_key": "", "model": "qwen.gguf"},
        },
        "active_provider": "openrouter",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key": "k-or",
        "model": "z-ai/glm-5.3-flash",
        "reasoning_effort": "",
        "max_tokens": 0,
        "temperature": 0.7,
    })

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["model"] = json["model"]
            captured["auth"] = headers.get("Authorization")
            raise RuntimeError("stop here")

    monkeypatch.setattr(mc.httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError, match="stop here"):
        await mc.chat([{"role": "user", "content": "hi"}],
                      model="llamacpp::qwen.gguf")
    assert captured["url"].startswith("http://192.168.1.170:8080/v1")
    assert captured["model"] == "qwen.gguf"
    assert "Authorization" not in (captured["auth"] or "")

    with pytest.raises(RuntimeError, match="stop here"):
        await mc.chat([{"role": "user", "content": "hi"}],
                      model="bare-id")
    assert captured["url"].startswith("https://openrouter.ai/api/v1")
    assert captured["model"] == "bare-id"
    assert captured["auth"] == "Bearer k-or"

    with pytest.raises(mc.ModelError, match="Unknown provider"):
        await mc.chat([{"role": "user", "content": "hi"}],
                      model="nope::m")


# ---------------------------------------------------- scheduled ask_user (#93)

@pytest.mark.asyncio
async def test_scheduled_run_skips_ask_user_without_opt_in(fake_model, tmp_path):
    """#93: a scheduled run (ANY policy) that has not opted into questions
    must never block on ask_user. The autonomous wedge: _wait_answer waited
    on the answer future alone, so an unattended run sat mid-step forever
    with no card (nothing renders for backend-initiated questions)."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t-sched-a")
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "q9", "type": "function",
            "function": {
                "name": "ask_user",
                "arguments": json.dumps({"question": "?", "options": []}),
            },
        }]},
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await collect(loop.run_agent(
        cid, "go", str(tmp_path), policy="autonomous"
    ))
    types = [e["type"] for e in events]
    assert types == ["tool_start", "tool_result", "text", "say", "done"]
    result = events[1]["result"]
    assert result["answer"] is None
    assert "no user is available" in result["note"]
    assert "decide yourself" in result["note"]


@pytest.mark.asyncio
async def test_scheduled_run_with_opt_in_waits_for_answer(fake_model, tmp_path):
    """#93: the agent-level allow_ask_user opt-in lets a scheduled run ask
    and block exactly like an interactive turn — the answer resolves it."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t-sched-b")
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "q3", "type": "function",
            "function": {
                "name": "ask_user",
                "arguments": json.dumps({
                    "question": "Proceed?",
                    "options": [{"label": "Yes"}, {"label": "No"}],
                }),
            },
        }]},
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    import asyncio
    agent = loop.run_agent(
        cid, "go", str(tmp_path), policy="autonomous", allow_ask_user=True
    )

    async def answer_when_asked():
        for _ in range(200):
            if loop.resolve_answer(cid, "q3", "Yes"):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("question never became pending")

    results = await asyncio.gather(collect(agent), answer_when_asked())
    events = results[0]
    assert events[1]["result"] == {"answer": "Yes"}
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_scheduled_exit_plan_never_waits_without_opt_in(fake_model, tmp_path):
    """#93 (same hole): exit_plan under a scheduled policy blocked on the
    answer future forever — an unattended run cannot present a plan."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t-sched-c")
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "p1", "type": "function",
            "function": {
                "name": "exit_plan",
                "arguments": json.dumps({"plan": "THE PLAN"}),
            },
        }]},
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await collect(loop.run_agent(
        cid, "go", str(tmp_path), policy="autonomous"
    ))
    result = next(e for e in events if e["type"] == "tool_result")["result"]
    assert "error" in result
    assert "unattended" in result["error"]
    assert events[-1]["type"] == "done"


# ---------------------------------------------- merge-back retry (#98/adr0002)


@pytest.mark.asyncio
async def test_dirty_turn_ends_and_state_survives_next_turn(fake_model, tmp_path, monkeypatch):
    """adr/0003: authored dirt no longer blocks the turn end — the turn
    ends (zero-commit noop, no pill), the session worktree keeps the
    dirt, and the NEXT turn of the same chat works in the SAME worktree
    (it can clean up there). Session end salvages what remains."""
    from backend.db.database import create_conversation

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "master")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    (repo / "hello.txt").write_text("v1\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    seen: list[str] = []
    marker_seen_by_turn2: list[bool] = []

    async def fake_execute(name, arguments, workspace, on_chunk=None):
        marker = Path(workspace) / "draft-notes.md"
        if seen:
            # turn 2's call: record what turn 1 left behind BEFORE cleanup
            marker_seen_by_turn2.append(marker.exists())
        if "clean" in str(arguments.get("command", "")):
            marker.unlink(missing_ok=True)
        elif not marker.exists():
            marker.write_text("# half-authored\n", encoding="utf-8")
        seen.append(workspace)
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(loop, "execute_tool", fake_execute)

    cid = await create_conversation("merge-retry")
    # Turn 1: dirties the worktree, then answers while dirty.
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "make dirt"})},
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "all done"}, {"type": "finish"}])
    # Turn 2 (same chat): cleans up, then finishes.
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c2",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "clean up"})},
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "clean now"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(repo)))
    texts = "".join(e.get("text", "") for e in events if e["type"] == "text")
    assert "all done" in texts, f"events={events}"

    # turn 1 ended with a zero-commit noop: NO pill, NO salvage, and the
    # session worktree survived with the dirt in place
    pills = [e for e in events if e.get("name") == "git_merge_back"]
    assert not pills, f"turn-end noop must not pill: {events}"
    leftovers = list((repo / ".yaah" / "worktrees").glob("*.salvage.patch"))
    assert not leftovers, "turn end must not salvage session state"
    assert len(seen) == 1 and ".yaah" in seen[0]

    # turn 2: SAME worktree (session binding), dirt still visible
    events2 = await collect(loop.run_agent(cid, "again", str(repo)))
    assert len(seen) == 2
    assert seen[1] == seen[0], "turn 2 must reuse the session worktree"
    assert marker_seen_by_turn2 == [True], (
        "turn 2 must see turn 1's uncommitted file"
    )
    texts2 = "".join(e.get("text", "") for e in events2 if e["type"] == "text")
    assert "clean now" in texts2
    # turn 2 ended clean with zero commits: the quiesced session DRAINS
    # (adr/0003 revised) — the worktree is gone; the next turn would run
    # on the main tree and re-isolate on demand.
    assert not Path(seen[1]).exists()
    # the session already drained at turn 2's end; a later chat deletion
    # release is a no-op, never an error
    from backend.agent import worktrees as worktrees_mod

    rel = await worktrees_mod.release_session(str(cid), why="test")
    assert rel.get("noop") is True


@pytest.mark.asyncio
async def test_stubborn_dirt_survives_turns_and_salvages_at_session_end(fake_model, tmp_path, monkeypatch):
    """adr/0003: a model that never cleans its worktree just leaves the
    dirt in the session worktree — every turn still merges commits, the
    session keeps the dirt across turns, and session end salvages it
    (never silently deletes)."""
    from backend.db.database import create_conversation

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "master")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    (repo / "hello.txt").write_text("v1\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    async def fake_execute(name, arguments, workspace, on_chunk=None):
        marker = Path(workspace) / "draft-notes.md"
        if not marker.exists():
            marker.write_text("# stubborn draft\n", encoding="utf-8")
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(loop, "execute_tool", fake_execute)

    cid = await create_conversation("merge-retry-limit")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "dirt"})},
            }],
        },
    ])
    # the model just keeps answering without cleaning up
    for i in range(5):
        fake_model.append([{"type": "content", "text": f"answer {i}"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(repo)))
    texts = "".join(e.get("text", "") for e in events if e["type"] == "text")
    # the turn ends normally (no nudge loop anymore): done, no pill
    assert "answer 0" in texts
    assert "done" in [e["type"] for e in events]
    pills = [e for e in events if e.get("name") == "git_merge_back"]
    assert not pills, f"zero-commit turn must not pill: {events}"
    # the stubborn draft is still in the session worktree (not salvaged,
    # not deleted — the next turn sees it)
    from backend.agent import worktrees as worktrees_mod

    info = worktrees_mod.binding_for(str(cid))
    assert info is not None, "session must stay bound after the turn"
    wt_path = Path(info["root"]) / ".yaah" / "worktrees" / str(cid)
    assert (wt_path / "draft-notes.md").exists(), (
        "stubborn dirt must survive the turn in the session worktree"
    )
    # session end: salvage (never silent deletion), worktree removed
    rel = await worktrees_mod.release_session(str(cid), why="test")
    salvages = list((repo / ".yaah" / "worktrees").glob("*.salvage.patch"))
    assert salvages and "stubborn draft" in salvages[0].read_text(encoding="utf-8")
    assert not wt_path.exists()
    assert rel["branch"] not in run_git(
        repo, "branch", "--list", rel["branch"]
    ).stdout


# --------------------------------------------------- mid-run branch visibility

@pytest.mark.asyncio
async def test_worktree_bound_released_events(fake_model, tmp_path, monkeypatch):
    """A turn that isolates emits worktree_bound (carrying the agent branch
    name) before its first write-tool result and worktree_released after the
    end-of-turn merge-back machinery — the branch chip's mid-run override and
    its revert-to-main. A read-only turn emits neither."""
    from backend.db.database import create_conversation

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "master")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    (repo / "hello.txt").write_text("v1\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    async def fake_execute(name, arguments, workspace, on_chunk=None):
        assert ".yaah" in workspace, "write tool must run inside the worktree"
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(loop, "execute_tool", fake_execute)

    # --- read-only turn: no worktree events at all
    cid = await create_conversation("wt-events-readonly")
    fake_model.append([
        {"type": "content", "text": "just reading"},
        {"type": "finish"},
    ])
    events = await collect(loop.run_agent(cid, "go", str(repo)))
    assert not [e for e in events if e["type"] == "worktree_bound"]
    assert not [e for e in events if e["type"] == "worktree_released"]

    # --- write turn: bound before the tool result, released at the end
    cid2 = await create_conversation("wt-events-write")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi > out.txt"})},
            }],
        },
        {"type": "content", "text": "wrote it"},
        {"type": "finish"},
    ])
    events2 = await collect(loop.run_agent(cid2, "go", str(repo)))

    bound = [e for e in events2 if e["type"] == "worktree_bound"]
    assert len(bound) == 1, f"expected exactly one bound event: {events2}"
    assert bound[0]["branch"].startswith("agent/"), bound[0]
    # bound arrives before the first write tool's result
    first_result = next(e for e in events2 if e["type"] == "tool_result")
    assert events2.index(bound[0]) < events2.index(first_result)

    released = [e for e in events2 if e["type"] == "worktree_released"]
    assert len(released) == 1, f"expected exactly one released event: {events2}"
    # released arrives in the finally block, after done
    assert events2.index(released[0]) > events2.index(
        next(e for e in events2 if e["type"] == "done")
    )


@pytest.mark.asyncio
async def test_worktree_isolation_note_and_status(monkeypatch, tmp_path):
    """Branch-first transparency: an isolated turn injects the
    `# Session worktree isolation` system note into the model's messages
    (it must never guess its location again), and turn end emits
    worktree_status with the branch, the worktree path, and the commit
    count ' instead of merging anything into the main tree."""
    from backend.db.database import create_conversation

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "master")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    (repo / "hello.txt").write_text("v1\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    seen_messages: list[list[dict]] = []
    seen_tools: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        seen_messages.append([dict(m) for m in messages])
        seen_tools.append([dict(t) for t in (tools or [])])
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    scripts: list[list[dict]] = []
    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    cid = await create_conversation("wt-note")
    scripts.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps(
                        {"command": "echo work > f.txt && git add f.txt && git commit -m w"}
                    ),
                },
            }],
        },
    ])
    scripts.append([{"type": "content", "text": "committed on my branch"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(repo)))

    # the model saw the isolation note as a system message
    notes = [
        m for call in seen_messages for m in call
        if m.get("role") == "system" and "Workspace integration" in str(m.get("content", ""))
    ]
    assert notes, "model must be told about its session worktree"
    note_text = notes[0]["content"]
    assert "agent/" in note_text and ".yaah" in note_text
    assert "git_merge_back" in note_text
    assert "Never force-push" in note_text
    assert "do not ask them to manage checkouts or branches" in note_text
    assert "integrate with `git_merge_back` before reporting complete" in note_text
    assert "Turn end itself never merges" in note_text
    assert "safe options with trade-offs" in note_text
    assert "Never say work is in main until the merge succeeds" in note_text
    assert "at end of turn the harness merges your committed work back" not in note_text

    # Tool descriptions are also model-facing prompt surface: keep them
    # accurate and avoid repeating the full release policy in git_push.
    schemas = {s["function"]["name"]: s["function"] for s in seen_tools[0]}
    merge_description = schemas["git_merge_back"]["description"]
    push_description = schemas["git_push"]["description"]
    assert "uncommitted target-workspace changes overlap" in merge_description
    assert "target tree is dirty" not in merge_description
    assert "Pushes the current branch" in push_description
    assert "clearly implied continuation" not in push_description

    # turn end: honest status event, nothing merged
    statuses = [e for e in events if e.get("type") == "worktree_status"]
    assert len(statuses) == 1
    st = statuses[0]
    assert st["commits"] == 1
    assert st["branch"].startswith("agent/")
    assert ".yaah" in st["worktree"]
    base_prompt = seen_messages[0][0]["content"]
    assert "Turn end itself never merges" in base_prompt
    assert "integrate them with `git_merge_back` before reporting" in base_prompt
    assert "do not ask the user to manage checkouts or merge routine work" in base_prompt
    assert "Classify dirty overlap, content conflict, or other refusal" in base_prompt
    assert "Offer safe options with" in base_prompt
    assert "routine tool calls need" in base_prompt
    assert "Do not ask again for decisions already stated" in base_prompt
    assert "at end of turn the harness merges your committed work back" not in base_prompt
    assert not (repo / "f.txt").exists(), "master must be untouched"
    # no fake git_merge_back pill at turn end any more
    assert not [e for e in events if e.get("name") == "git_merge_back" and e.get("type") == "tool_result"]