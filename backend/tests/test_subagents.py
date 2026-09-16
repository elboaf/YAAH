"""Tests for the sub-agent framework (model is faked)."""
import asyncio
import json

import pytest

from backend.agent import loop, subagents
from backend.agent.subagents import AgentDef


# ---------------------------------------------------------------- registry


def test_builtins_present():
    names = {d["name"] for d in subagents.list_agents()}
    assert "general-purpose" in names
    assert "explore" in names


def test_explore_is_read_only():
    ex = subagents.get_agent_def("explore")
    tools = {s["function"]["name"] for s in subagents._resolve_tools(ex, windows=True)}
    for forbidden in ("bash", "write_file", "edit_file", "create_file",
                      "delete_file", "move_file", "git_add", "git_commit",
                      "git_push", "git_pull", "powershell"):
        assert forbidden not in tools, forbidden
    for required in ("read_file", "search_files", "web_search", "git_status"):
        assert required in tools, required


def test_general_purpose_excludes_ask_user_and_spawn():
    gp = subagents.get_agent_def("general-purpose")
    tools = {s["function"]["name"] for s in subagents._resolve_tools(gp, windows=True)}
    assert "ask_user" not in tools
    assert "spawn_agent" not in tools
    assert "bash" in tools
    assert "write_file" in tools


def test_computer_tools_never_reach_subagents():
    gp = subagents.get_agent_def("general-purpose")
    tools = {s["function"]["name"] for s in subagents._resolve_tools(gp, windows=True)}
    for forbidden in ("screenshot", "mouse_click", "type_text", "read_ui_tree"):
        assert forbidden not in tools, forbidden


def test_custom_agent_definition(tmp_path, monkeypatch):
    (tmp_path / "code-reviewer.md").write_text(
        "---\n"
        "name: code-reviewer\n"
        "description: Reviews code for bugs.\n"
        "tools: [read_file, search_files]\n"
        "maxTurns: 12\n"
        "---\n"
        "You are a code review specialist.",
        encoding="utf-8",
    )
    monkeypatch.setattr(subagents, "AGENTS_DIR", tmp_path)
    subagents.scan_agents()
    d = subagents.get_agent_def("code-reviewer")
    assert d is not None
    assert d.max_turns == 12
    tools = {s["function"]["name"] for s in subagents._resolve_tools(d, windows=True)}
    assert tools == {"read_file", "search_files"}


def test_builtin_name_cannot_be_shadowed(tmp_path, monkeypatch):
    (tmp_path / "explore.md").write_text(
        "---\nname: explore\ndescription: fake\n---\nbody",
        encoding="utf-8",
    )
    monkeypatch.setattr(subagents, "AGENTS_DIR", tmp_path)
    subagents.scan_agents()
    d = subagents.get_agent_def("explore")
    assert d.builtin is True


def test_broken_agent_file_is_skipped(tmp_path, monkeypatch):
    (tmp_path / "broken.md").write_text("---\ndescription: no name\n---\nbody",
                                        encoding="utf-8")
    (tmp_path / "badyaml.md").write_text("---\n[unclosed\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(subagents, "AGENTS_DIR", tmp_path)
    subagents.scan_agents()
    assert subagents.get_agent_def("broken") is None


def test_index_for_prompt_lists_agents():
    idx = subagents.index_for_prompt()
    assert "general-purpose" in idx
    assert "explore" in idx
    assert "spawn_agent" in idx


# ---------------------------------------------------------------- runner


class FakeStream:
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
    """Scripted responses per model_client.chat call."""
    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(subagents.model_client, "chat", fake_chat)
    return scripts


@pytest.mark.asyncio
async def test_sub_agent_direct_answer(fake_model, tmp_path):
    fake_model.append([
        {"type": "content", "text": "Research done: found 3 issues."},
        {"type": "finish"},
    ])
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "find issues", str(tmp_path)
    )
    assert result["status"] == "completed"
    assert result["output"] == "Research done: found 3 issues."
    assert result["turns"] == 1
    roles = [e["role"] for e in result["transcript"]]
    assert roles == ["user", "assistant"]


@pytest.mark.asyncio
async def test_sub_agent_runs_tools(fake_model, tmp_path):
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    ])
    fake_model.append([
        {"type": "content", "text": "file says hello"},
        {"type": "finish"},
    ])
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "read a.txt", str(tmp_path)
    )
    assert result["status"] == "completed"
    assert result["output"] == "file says hello"
    roles = [e["role"] for e in result["transcript"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_entry = result["transcript"][2]
    assert tool_entry["tool_call_id"] == "c1"
    assert "hello" in tool_entry["content"]


@pytest.mark.asyncio
async def test_sub_agent_cannot_ask_user(fake_model, tmp_path):
    """A sub-agent calling ask_user gets an error result, not a hang."""
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "ask_user", "arguments": "{}"},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    ])
    fake_model.append([
        {"type": "content", "text": "decided myself"},
        {"type": "finish"},
    ])
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("general-purpose"), "decide something", str(tmp_path)
    )
    assert result["status"] == "completed"
    assert result["output"] == "decided myself"
    tool_entry = result["transcript"][2]
    assert "error" in json.loads(tool_entry["content"])


@pytest.mark.asyncio
async def test_sub_agent_cannot_spawn(fake_model, tmp_path):
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "spawn_agent", "arguments": "{}"},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    ])
    fake_model.append([
        {"type": "content", "text": "gave up on nesting"},
        {"type": "finish"},
    ])
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("general-purpose"), "try to nest", str(tmp_path)
    )
    assert result["status"] == "completed"
    tool_entry = result["transcript"][2]
    assert "error" in json.loads(tool_entry["content"])


@pytest.mark.asyncio
async def test_sub_agent_max_turns(fake_model, tmp_path):
    defn = AgentDef(name="t", description="", body="b", max_turns=2)
    for _ in range(2):
        fake_model.append([
            {"type": "tool_calls", "tool_calls": [{
                "id": "c", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
            }]},
            {"type": "finish", "reason": "tool_calls"},
        ])
    result = await subagents.run_sub_agent(defn, "loop forever", str(tmp_path))
    assert result["status"] == "max_turns"
    assert result["turns"] == 2


@pytest.mark.asyncio
async def test_sub_agent_model_error_is_contained(fake_model, tmp_path, monkeypatch):
    async def failing_chat(messages, tools=None, stream=True):
        raise subagents.model_client.ModelError("api down")

    monkeypatch.setattr(subagents.model_client, "chat", failing_chat)
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("explore"), "anything", str(tmp_path)
    )
    assert result["status"] == "error"
    assert "api down" in result["error"]
    # The parent still gets a usable (if empty) output field.
    assert "output" in result


@pytest.mark.asyncio
async def test_sub_agent_cancellation_keeps_partial_transcript(tmp_path):
    """Cancel mid-run: status='cancelled', partial transcript preserved."""
    import backend.agent.subagents as sa

    calls = {"n": 0}

    async def mixed_chat(messages, tools=None, stream=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeStream([
                {"type": "tool_calls", "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }]},
                {"type": "finish", "reason": "tool_calls"},
            ])
        ev = asyncio.Event()
        await ev.wait()  # blocks until cancelled

    real_chat = sa.model_client.chat
    sa.model_client.chat = mixed_chat  # type: ignore
    try:
        cancel_ev = asyncio.Event()
        task = asyncio.create_task(
            subagents.run_sub_agent(
                subagents.get_agent_def("explore"), "x", str(tmp_path),
                cancel_ev=cancel_ev,
            )
        )
        await asyncio.sleep(0.05)
        cancel_ev.set()
        result = await asyncio.wait_for(task, timeout=5)
    finally:
        sa.model_client.chat = real_chat

    assert result["status"] == "cancelled"
    # The first turn's tool call is in the transcript.
    roles = [e["role"] for e in result["transcript"]]
    assert "tool" in roles


@pytest.mark.asyncio
async def test_spawn_batch_parallel_and_capped(fake_model, tmp_path):
    """Three calls run concurrently; results map back by call_id."""
    for i in range(3):
        fake_model.append([
            {"type": "content", "text": f"result {i}"},
            {"type": "finish"},
        ])
    calls = [
        {"call_id": f"c{i}", "agent_id": i, "agent_type": "explore", "prompt": f"task {i}"}
        for i in range(3)
    ]
    results = await subagents.spawn_batch(calls, str(tmp_path), asyncio.Event())
    assert set(results) == {"c0", "c1", "c2"}
    for i in range(3):
        assert results[f"c{i}"]["output"] == f"result {i}"


@pytest.mark.asyncio
async def test_spawn_batch_unknown_agent_type(fake_model, tmp_path):
    calls = [{"call_id": "c0", "agent_id": 0, "agent_type": "nope", "prompt": "x"}]
    results = await subagents.spawn_batch(calls, str(tmp_path), asyncio.Event())
    assert "error" in results["c0"]
    assert "available" in results["c0"]


@pytest.mark.asyncio
async def test_spawn_batch_empty_prompt(fake_model, tmp_path):
    calls = [{"call_id": "c0", "agent_id": 0, "agent_type": "explore", "prompt": "  "}]
    results = await subagents.spawn_batch(calls, str(tmp_path), asyncio.Event())
    assert "error" in results["c0"]


# ---------------------------------------------------------------- loop integration


async def collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


@pytest.fixture
def fake_model_single(monkeypatch):
    """One scripted stream for BOTH the parent loop and sub-agents — they
    share the same model_client module, so a single dispatching fake is
    the only correct patch. Scripts are consumed in call order."""
    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


@pytest.mark.asyncio
async def test_parent_delegates_and_receives_summary(fake_model_single, tmp_path):
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("t")
    # Call 1: parent decides to spawn
    fake_model_single.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "p1", "type": "function",
            "function": {"name": "spawn_agent", "arguments": json.dumps({
                "agent_type": "explore", "prompt": "find the bug",
            })},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    ])
    # Call 2: sub-agent answers
    fake_model_single.append([
        {"type": "content", "text": "the bug is in loop.py line 42"},
        {"type": "finish"},
    ])
    # Call 3: parent's final answer
    fake_model_single.append([
        {"type": "content", "text": "Found it: the bug is in loop.py line 42"},
        {"type": "finish"},
    ])

    events = await collect(loop.run_agent(cid, "find the bug", str(tmp_path)))
    types = [e["type"] for e in events]
    assert "sub_agent_spawned" in types
    assert "sub_agent_progress" in types
    assert "sub_agent_done" in types
    assert "tool_result" in types
    assert "done" in types

    # The parent's tool result holds the summary, not the transcript.
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["result"]["output"] == "the bug is in loop.py line 42"
    assert "transcript" not in tr["result"]

    # Persisted row carries the transcript snapshot.
    rows = await get_messages(cid)
    tool_rows = [r for r in rows if r["role"] == "tool"]
    assert tool_rows[-1]["sub_agent_transcript"] is not None
    snap = tool_rows[-1]["sub_agent_transcript"]
    assert snap["output"] == "the bug is in loop.py line 42"
    assert isinstance(snap["transcript"], list)


@pytest.mark.asyncio
async def test_parent_transcript_not_replayed_into_history(fake_model_single, tmp_path, monkeypatch):
    """A later turn sees the sub-agent's final message (it IS the tool
    result) but never the transcript's intermediate entries."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t")
    fake_model_single.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "p1", "type": "function",
            "function": {"name": "spawn_agent", "arguments": json.dumps({
                "agent_type": "explore", "prompt": "research",
            })},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    ])
    # Sub-agent turn 1: reads a file (its result must stay sub-agent-only)
    fake_model_single.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "s1", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "secret.txt"})},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    ])
    # Sub-agent turn 2: final answer
    fake_model_single.append([
        {"type": "content", "text": "findings summary"},
        {"type": "finish"},
    ])
    # Parent's final answer
    fake_model_single.append([
        {"type": "content", "text": "done"},
        {"type": "finish"},
    ])
    (tmp_path / "secret.txt").write_text("INTERMEDIATE-TOOL-CONTENT", encoding="utf-8")
    await collect(loop.run_agent(cid, "go", str(tmp_path)))

    seen = {}

    async def spy_chat(messages, tools=None, stream=True):
        seen["messages"] = messages
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", spy_chat)
    await collect(loop.run_agent(cid, "and now?", str(tmp_path)))

    dumped = json.dumps(seen["messages"])
    # The sub-agent's intermediate tool result never enters parent history.
    assert "INTERMEDIATE-TOOL-CONTENT" not in dumped
    # The sub-agent's final message DOES (it is the parent's tool result).
    assert "findings summary" in dumped


@pytest.mark.asyncio
async def test_parent_cancel_cancels_sub_agents(tmp_path, monkeypatch):
    """Stop during a spawn batch cancels children and ends the turn."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t")

    calls = {"n": 0}

    async def spawn_then_block(messages, tools=None, stream=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeStream([
                {"type": "tool_calls", "tool_calls": [{
                    "id": "p1", "type": "function",
                    "function": {"name": "spawn_agent", "arguments": json.dumps({
                        "agent_type": "explore", "prompt": "long research",
                    })},
                }]},
                {"type": "finish", "reason": "tool_calls"},
            ])
        ev = asyncio.Event()
        await ev.wait()  # sub-agent model call blocks until cancelled

    monkeypatch.setattr(loop.model_client, "chat", spawn_then_block)
    gen = loop.run_agent(cid, "go", str(tmp_path))
    first = await gen.__anext__()
    assert json.loads(first)["type"] == "tool_start"
    # Let the batch start, then cancel.
    await asyncio.sleep(0.1)
    loop.cancel_agent(cid)
    events = []
    async for e in gen:
        events.append(json.loads(e))
    types = [e["type"] for e in events]
    assert "stopped" in types


@pytest.mark.asyncio
async def test_stream_abort_persists_partial_transcripts(tmp_path, monkeypatch):
    """The Stop-button path: the HTTP stream aborts, tearing down the
    generator mid-batch. The grace-window handler must cancel the batch,
    collect each sub-agent's partial result, and persist a 'cancelled'
    tool row with its transcript — instead of leaving nothing behind.
    (Regression for the stuck-pulsing spawn_agent chips.)"""
    import backend.agent.subagents as sa
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("t")

    calls = {"n": 0}

    async def spawn_then_block(messages, tools=None, stream=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeStream([
                {"type": "tool_calls", "tool_calls": [{
                    "id": "p1", "type": "function",
                    "function": {"name": "spawn_agent", "arguments": json.dumps({
                        "agent_type": "explore", "prompt": "long research",
                    })},
                }]},
                {"type": "finish", "reason": "tool_calls"},
            ])
        # Sub-agent: emit one text delta, then block until the cancel
        # event interrupts the stream — the partial-run shape.
        if calls["n"] == 2:

            async def partial_stream():
                yield {"type": "content", "text": "partial findings before stop"}
                ev = asyncio.Event()
                await ev.wait()

            return partial_stream()
        ev = asyncio.Event()
        await ev.wait()

    monkeypatch.setattr(sa.model_client, "chat", spawn_then_block)

    # Simulate the server: run_agent consumed by a request handler whose
    # stream is aborted client-side mid-iteration (Stop button).
    gen = loop.run_agent(cid, "go", str(tmp_path))
    first = await gen.__anext__()
    assert json.loads(first)["type"] == "tool_start"
    await asyncio.sleep(0.15)  # let the sub-agent start and stream its text

    async def aborting_consumer():
        async for _ in gen:
            pass

    consumer = asyncio.create_task(aborting_consumer())
    await asyncio.sleep(0.1)
    consumer.cancel()  # the abort: GeneratorExit inside run_agent
    try:
        await consumer
    except asyncio.CancelledError:
        pass
    # Drain the generator so its CancelledError/GeneratorExit handler runs.
    try:
        async for _ in gen:
            pass
    except (asyncio.CancelledError, GeneratorExit):
        pass

    # Whatever the teardown produced must be persisted and terminal.
    rows = await get_messages(cid)
    tool_rows = [r for r in rows if r["role"] == "tool" and r["tool_call_id"] == "p1"]
    assert tool_rows, "interrupted spawn_agent left no persisted row"
    snap = tool_rows[-1]["sub_agent_transcript"]
    assert snap is not None
    assert snap["status"] in ("cancelled", "completed")
    # Partial work survives in the transcript when the sub-agent got far
    # enough to stream before the cancel event landed.
    transcript_text = json.dumps(snap.get("transcript") or [])
    assert "partial findings before stop" in transcript_text


@pytest.mark.asyncio
async def test_parent_survives_sub_agent_failure(fake_model_single, tmp_path, monkeypatch):
    """A sub-agent that errors returns a structured result; parent continues."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t")
    fake_model_single.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "p1", "type": "function",
            "function": {"name": "spawn_agent", "arguments": json.dumps({
                "agent_type": "explore", "prompt": "x",
            })},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    ])
    fake_model_single.append([
        {"type": "content", "text": "handled the failure"},
        {"type": "finish"},
    ])

    import backend.agent.subagents as sa

    calls = {"n": 0}

    async def failing_then_ok(messages, tools=None, stream=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeStream([
                {"type": "tool_calls", "tool_calls": [{
                    "id": "p1", "type": "function",
                    "function": {"name": "spawn_agent", "arguments": json.dumps({
                        "agent_type": "explore", "prompt": "x",
                    })},
                }]},
                {"type": "finish", "reason": "tool_calls"},
            ])
        if calls["n"] == 2:
            raise sa.model_client.ModelError("boom")  # the sub-agent's call
        return FakeStream([
            {"type": "content", "text": "handled the failure"},
            {"type": "finish"},
        ])  # the parent's final call

    monkeypatch.setattr(sa.model_client, "chat", failing_then_ok)
    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    types = [e["type"] for e in events]
    assert "error" not in types  # parent turn did not fail
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["result"]["status"] == "error"
    assert "boom" in tr["result"]["error"]
    done = [e for e in events if e["type"] == "done"]
    assert done


@pytest.mark.asyncio
async def test_spawn_agent_costs_parent_one_step(fake_model_single, tmp_path):
    """A spawn_agent call is one tool call in one parent step."""
    from backend.db.database import create_conversation

    cid = await create_conversation("t")
    fake_model_single.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "p1", "type": "function",
            "function": {"name": "spawn_agent", "arguments": json.dumps({
                "agent_type": "explore", "prompt": "x",
            })},
        }]},
        {"type": "finish", "reason": "tool_calls"},
    ])
    fake_model_single.append([
        {"type": "content", "text": "sub result"},
        {"type": "finish"},
    ])
    fake_model_single.append([
        {"type": "content", "text": "done"},
        {"type": "finish"},
    ])
    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    # Parent made exactly 2 model calls: one with the spawn, one final.
    # The sub-agent's text ("sub result") is NOT a parent text event — it
    # arrives wrapped as sub_agent_progress.
    parent_texts = [e for e in events if e["type"] == "text"]
    assert len(parent_texts) == 1
    progress = [e for e in events if e["type"] == "sub_agent_progress"]
    assert any(e.get("text") == "sub result" for e in progress)
