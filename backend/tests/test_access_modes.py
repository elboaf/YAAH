"""Access modes: the tool-approval gate (PLAN-access-modes.md).

Covers classification, ask-mode blocking/resolution, plan-mode blocking,
sub-agent coverage, and config round-trip. The gate reuses the ask_user
future machinery, so the ask-mode tests follow the ask_user test pattern
in test_agent.py.
"""
import asyncio
import json

import pytest

from backend.agent import loop
from backend.agent.tools import tool_risk


# ---------------------------------------------------------------- classify


def test_read_tools_are_free_in_every_mode():
    for name in ("read_file", "search_files", "git_status", "git_diff",
                 "web_search", "web_fetch", "view_image", "load_skill",
                 "screenshot", "list_windows", "read_ui_tree", "wait",
                 "spawn_agent"):
        assert tool_risk(name) == "read", name


def test_mutating_tools_ask():
    for name in ("write_file", "edit_file", "create_file", "delete_file",
                 "move_file", "git_add", "git_commit"):
        assert tool_risk(name) == "mutating", name


def test_shell_tools_ask():
    for name in ("bash", "powershell", "git_push", "git_pull",
                 "mouse_click", "type_text", "press_key", "focus_window"):
        assert tool_risk(name) == "shell", name


def test_unknown_and_mcp_tools_classify_as_shell():
    """Safe by default: an unclassified tool (MCP, future) prompts under
    ask mode and blocks under plan mode."""
    assert tool_risk("mcp_some_server_dangerous") == "shell"
    assert tool_risk("totally_unknown_tool") == "shell"


# ---------------------------------------------------------------- gate unit


@pytest.mark.asyncio
async def test_gate_passes_reads_and_full_mode(tmp_path):
    """Read tools pass in every mode; full mode passes everything."""
    cancel = asyncio.Event()
    assert await loop.run_gate("read_file", {}, "c1", 1, cancel, mode="ask") is None
    assert await loop.run_gate("read_file", {}, "c1", 1, cancel, mode="plan") is None
    assert await loop.run_gate("bash", {}, "c1", 1, cancel, mode="full") is None
    assert await loop.run_gate("write_file", {}, "c1", 1, cancel, mode="full") is None


@pytest.mark.asyncio
async def test_gate_plan_mode_blocks_without_executing(tmp_path):
    cancel = asyncio.Event()
    result = await loop.run_gate("write_file", {"path": "x.txt"}, "c1", 1, cancel, mode="plan")
    assert result is not None and "error" in result
    assert "plan mode" in result["error"]
    # bash too
    result = await loop.run_gate("bash", {"command": "echo hi"}, "c1", 1, cancel, mode="plan")
    assert result is not None and "plan mode" in result["error"]


@pytest.mark.asyncio
async def test_gate_ask_mode_blocks_until_resolved(tmp_path):
    """The gate blocks on the future; approve executes (None), deny returns
    the denial error, free text denies with guidance."""
    cancel = asyncio.Event()
    task = asyncio.create_task(
        loop.run_gate("bash", {"command": "echo hi"}, "c1", 1, cancel, mode="ask")
    )
    # Wait for the future to register.
    for _ in range(200):
        if "1:c1" in loop._pending_answers:
            break
        await asyncio.sleep(0.01)
    assert loop.resolve_answer(1, "c1", "approve")
    assert await task is None

    task = asyncio.create_task(
        loop.run_gate("bash", {"command": "echo hi"}, "c2", 1, cancel, mode="ask")
    )
    for _ in range(200):
        if "1:c2" in loop._pending_answers:
            break
        await asyncio.sleep(0.01)
    assert loop.resolve_answer(1, "c2", "deny")
    result = await task
    assert result is not None and "denied" in result["error"]

    task = asyncio.create_task(
        loop.run_gate("bash", {"command": "echo hi"}, "c3", 1, cancel, mode="ask")
    )
    for _ in range(200):
        if "1:c3" in loop._pending_answers:
            break
        await asyncio.sleep(0.01)
    assert loop.resolve_answer(1, "c3", "use python instead")
    result = await task
    assert result is not None and "use python instead" in result["error"]


@pytest.mark.asyncio
async def test_gate_cancel_unblocks_with_error(tmp_path):
    cancel = asyncio.Event()
    task = asyncio.create_task(
        loop.run_gate("bash", {"command": "echo hi"}, "c1", 1, cancel, mode="ask")
    )
    for _ in range(200):
        if "1:c1" in loop._pending_answers:
            break
        await asyncio.sleep(0.01)
    cancel.set()
    result = await task
    assert result is not None and "error" in result


# ---------------------------------------------------------------- loop e2e


class FakeStream:
    """Async iterator over preset events (same shape as test_agent's)."""

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


@pytest.mark.asyncio
async def test_ask_mode_turn_blocks_and_denial_continues(fake_model, tmp_path, monkeypatch):
    """Full loop: in ask mode a write_file emits approval_request, blocks,
    and a denial lets the turn continue with the error result."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cfgmod, "DEFAULTS", {**cfgmod.DEFAULTS, "access_mode": "ask"})
    # current_access_mode reads load_config() -> CONFIG_PATH (patched).
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("t-gate")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "w1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "a.txt", "content": "hi"}),
                },
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    agent = loop.run_agent(cid, "go", str(tmp_path))

    async def deny_when_asked():
        for _ in range(200):
            if loop.resolve_answer(cid, "w1", "deny"):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("approval never became pending")

    results = await asyncio.gather(_collect(agent), deny_when_asked())
    events = results[0]
    types = [e["type"] for e in events]
    assert "approval_request" in types
    assert "approval_decision" in types
    decision = next(e for e in events if e["type"] == "approval_decision")
    assert decision["approved"] is False
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert "denied" in json.dumps(tool_result["result"])
    # The file must NOT exist.
    assert not (tmp_path / "a.txt").exists()


async def _collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


@pytest.mark.asyncio
async def test_ask_mode_approval_executes_tool(fake_model, tmp_path, monkeypatch):
    """Approve executes the tool for real."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    from backend.db.database import create_conversation

    cid = await create_conversation("t-gate2")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "w1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "a.txt", "content": "hi"}),
                },
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    agent = loop.run_agent(cid, "go", str(tmp_path))

    async def approve_when_asked():
        for _ in range(200):
            if loop.resolve_answer(cid, "w1", "approve"):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("approval never became pending")

    results = await asyncio.gather(_collect(agent), approve_when_asked())
    events = results[0]
    decision = next(e for e in events if e["type"] == "approval_decision")
    assert decision["approved"] is True
    assert (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_plan_mode_blocks_in_loop_and_prompt_notes_it(fake_model, tmp_path, monkeypatch):
    """Plan mode: write_file returns the plan-mode error without executing;
    the system prompt carries the plan-mode section."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    # Capture the system prompt via the model client's messages arg.
    captured = {}
    scripts: list[list[dict]] = [
        [
            {
                "type": "tool_calls",
                "tool_calls": [{
                    "id": "w1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "a.txt", "content": "hi"}),
                    },
                }],
            },
        ],
        [{"type": "content", "text": "plan: I will write a.txt"}, {"type": "finish"}],
    ]

    async def fake_chat(messages, tools=None, stream=True):
        captured["system"] = messages[0]["content"]
        return FakeStream(scripts.pop(0))

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    from backend.db.database import create_conversation

    cid = await create_conversation("t-plan")
    # Write the config with plan mode BEFORE the turn.
    cfgmod.save_config({"access_mode": "plan"})

    events = await _collect(loop.run_agent(cid, "go", str(tmp_path)))
    types = [e["type"] for e in events]
    assert "approval_request" not in types
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert "plan mode" in json.dumps(tool_result["result"])
    assert "Access mode: PLAN" in captured["system"]
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_sub_agent_tool_calls_hit_the_gate(fake_model, tmp_path, monkeypatch):
    """A sub-agent's write_file passes through the parent's gate: in ask
    mode it blocks until the user resolves the namespaced key."""
    from backend.agent import config as cfgmod
    from backend.agent import subagents

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "sub.txt", "content": "x"}),
                },
            }],
        },
        {"type": "finish", "reason": "tool_calls"},
    ])
    fake_model.append([
        {"type": "content", "text": "wrote it"},
        {"type": "finish"},
    ])

    cancel = asyncio.Event()
    gate_calls = []

    async def gate(name, args, call_id):
        gate_calls.append((name, call_id))
        # Deny: the sub-agent must see the error, not the file write.
        return {"error": "user denied the write_file tool call"}

    result = await subagents.run_sub_agent(
        subagents.get_agent_def("general-purpose"),
        "write a file",
        str(tmp_path),
        cancel_ev=cancel,
        gate=gate,
        on_event=lambda ev: None,
    )
    assert result["status"] == "completed"
    assert gate_calls == [("write_file", "c1")]
    tool_entry = result["transcript"][2]
    assert "denied" in tool_entry["content"]
    assert not (tmp_path / "sub.txt").exists()


# ---------------------------------------------------------------- config API


def test_config_roundtrip_and_validation(tmp_path, monkeypatch):
    """access_mode persists and invalid values fall back to ask."""
    from fastapi.testclient import TestClient

    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    import backend.main as mainmod

    monkeypatch.setattr(mainmod, "load_config", cfgmod.load_config)
    from backend.main import app

    with TestClient(app) as client:
        r = client.get("/api/config")
        assert r.status_code == 200
        assert r.json()["access_mode"] == "ask"  # default

        r = client.put("/api/config", json={"access_mode": "plan"})
        assert r.status_code == 200
        assert client.get("/api/config").json()["access_mode"] == "plan"

        # Invalid falls back to ask (both at the API and the gate).
        r = client.put("/api/config", json={"access_mode": "yolo"})
        assert r.status_code == 200
        assert client.get("/api/config").json()["access_mode"] == "ask"
        assert loop.current_access_mode() == "ask"


# ---------------------------------------------------------------- exit_plan


@pytest.mark.asyncio
async def test_exit_plan_approval_ends_plan_mode_and_resumes(fake_model, tmp_path, monkeypatch):
    """The exit_plan call blocks the run; approving flips the saved access
    mode to full and the SAME turn continues — the follow-up write_file
    executes without another approval round-trip."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "p1",
                "type": "function",
                "function": {
                    "name": "exit_plan",
                    "arguments": json.dumps({"plan": "I will write a.txt"}),
                },
            }],
        },
    ])
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "w1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "a.txt", "content": "hi"}),
                },
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    cfgmod.save_config({"access_mode": "plan"})
    from backend.db.database import create_conversation

    cid = await create_conversation("t-exit-plan")

    agent = loop.run_agent(cid, "go", str(tmp_path))

    async def approve_when_asked():
        for _ in range(200):
            if loop.resolve_answer(cid, "p1", "approve"):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("exit_plan never became pending")

    results = await asyncio.gather(_collect(agent), approve_when_asked())
    events = results[0]
    plan_result = next(e for e in events if e["type"] == "tool_result" and e["name"] == "exit_plan")
    assert plan_result["result"]["decision"] == "approved"
    # The write executed in the same turn: no approval_request was ever
    # needed (full mode passes the gate) and the file exists.
    types = [e["type"] for e in events]
    assert "approval_request" not in types
    assert (tmp_path / "a.txt").exists()
    # The saved mode is now full (backend-side, not just the store).
    assert cfgmod.load_config()["access_mode"] == "full"


@pytest.mark.asyncio
async def test_exit_plan_feedback_keeps_plan_mode(fake_model, tmp_path, monkeypatch):
    """Typed feedback resolves the call as a change request: plan mode stays
    on and the model gets the feedback as the tool result."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "p1",
                "type": "function",
                "function": {
                    "name": "exit_plan",
                    "arguments": json.dumps({"plan": "v1"}),
                },
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "revised"}, {"type": "finish"}])

    cfgmod.save_config({"access_mode": "plan"})
    from backend.db.database import create_conversation

    cid = await create_conversation("t-exit-plan2")
    agent = loop.run_agent(cid, "go", str(tmp_path))

    async def send_feedback():
        for _ in range(200):
            if loop.resolve_answer(cid, "p1", "use less files"):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("exit_plan never became pending")

    results = await asyncio.gather(_collect(agent), send_feedback())
    plan_result = next(
        e for e in results[0] if e["type"] == "tool_result" and e["name"] == "exit_plan"
    )
    assert plan_result["result"]["decision"] == "revised"
    assert plan_result["result"]["feedback"] == "use less files"
    assert cfgmod.load_config()["access_mode"] == "plan"


@pytest.mark.asyncio
async def test_exit_plan_unit_paths(tmp_path, monkeypatch):
    """Unit: cancel unblocks; calling outside plan mode is an error; a blank
    plan is rejected without blocking."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    # Outside plan mode: immediate error, nothing pending.
    result = await loop._exit_plan(1, "c0", {"plan": "p"}, asyncio.Event())
    assert "error" in result
    assert not loop._pending_answers

    cfgmod.save_config({"access_mode": "plan"})

    # Blank plan rejected without registering a future.
    result = await loop._exit_plan(1, "c1", {}, asyncio.Event())
    assert "error" in result
    assert not loop._pending_answers

    # Cancel while blocked.
    cancel = asyncio.Event()
    task = asyncio.create_task(loop._exit_plan(1, "c2", {"plan": "p"}, cancel))
    for _ in range(200):
        if "1:c2" in loop._pending_answers:
            break
        await asyncio.sleep(0.01)
    cancel.set()
    result = await task
    assert result["decision"] == "cancelled"

    # Approve flips the saved mode before the caller resumes.
    task = asyncio.create_task(loop._exit_plan(1, "c3", {"plan": "p"}, asyncio.Event()))
    for _ in range(200):
        if "1:c3" in loop._pending_answers:
            break
        await asyncio.sleep(0.01)
    assert loop.resolve_answer(1, "c3", "approve")
    result = await task
    assert result["decision"] == "approved"
    assert cfgmod.load_config()["access_mode"] == "full"


def test_exit_plan_schema_only_injected_in_plan_mode(tmp_path, monkeypatch):
    """get_schemas() itself never carries exit_plan; the loop appends it
    only while plan mode is on."""
    from backend.agent import config as cfgmod
    from backend.agent.tools import get_schemas

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)
    names = {s["function"]["name"] for s in get_schemas()}
    assert "exit_plan" not in names
