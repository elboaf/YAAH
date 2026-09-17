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
