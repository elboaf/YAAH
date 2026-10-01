"""Sandbox-only policy note accuracy (issue #177).

The note injected for sandbox-only scheduled runs must state the ACTUAL
gate contract, which is enumerable from tool_risk(): read-only tools
execute; everything else (mutating + shell, which includes bash and the
mutating sandbox_* tools) skips with "skipped: approval required". The
old wording claimed "every tool that normally requires user approval ...
is unavailable", which both overstates the gate (read-only tools run)
and never mentions that the sandbox VM tools are covered by it.
"""

import json

import pytest

from backend.agent import loop
from backend.agent.tools import tool_risk
from backend.db.database import create_conversation

SANDBOX_TOOLS = (
    "sandbox_test", "sandbox_run", "sandbox_status", "sandbox_stop",
)


def test_note_matches_gate_contract():
    """The note names the real rule: read-only tools run, everything else
    skips — and it explicitly covers the sandbox VM tools."""
    note = loop._sandbox_only_note()
    assert "read-only" in note
    assert "sandbox" in note
    assert "skipped: approval required" in note
    # The old overclaim must not come back.
    assert "every tool that normally requires user approval" not in note
    assert "unavailable" not in note


@pytest.mark.parametrize(
    "name", ("sandbox_test", "sandbox_run", "sandbox_stop")
)
def test_mutating_sandbox_tools_gate_by_risk(name):
    """The note's claim is enumerable from the gate: the three mutating
    sandbox tools classify non-read, so they skip under sandbox-only."""
    assert tool_risk(name) != "read"


def test_sandbox_status_is_read_only_and_runs():
    """sandbox_status classifies 'read', so it still executes — the note
    correctly tells the model read-only observation remains available."""
    assert tool_risk("sandbox_status") == "read"


@pytest.mark.asyncio
async def test_sandbox_only_policy_injects_corrected_note(tmp_path, monkeypatch):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    cid = await create_conversation("t")
    captured = {}

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        captured["system"] = messages[0]["content"]

        async def _stream():
            yield {"type": "content", "text": "done"}
            yield {"type": "finish"}
        return _stream()

    orig_chat = loop.model_client.chat
    loop.model_client.chat = fake_chat
    try:
        async for _ in loop.run_agent(
            cid, "go", str(tmp_path), policy="sandbox-only"
        ):
            pass
    finally:
        loop.model_client.chat = orig_chat

    assert "# Scheduled agent: sandbox-only policy" in captured["system"]
    assert "read-only" in captured["system"]
