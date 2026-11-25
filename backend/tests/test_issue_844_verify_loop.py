"""#844 Stage C: gate completion on the post-turn verifier, loop with
nextAction. Drives the real agent loop with a faked model client.
"""
import asyncio
import json

import pytest

from backend.agent import loop
from backend.agent import config as cfgmod


class FakeStream:
    """Async iterator over preset stream events."""

    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


async def collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


@pytest.mark.asyncio
async def test_verifier_blocks_done_until_passing(tmp_path, monkeypatch):
    """A failed verifier must NOT emit done — it surfaces a `goal` iteration
    title, loops, and only emits done once the verifier passes."""
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("verify me")
    ws = str(tmp_path)

    turn_scripts: asyncio.Queue = asyncio.Queue()
    verdicts: asyncio.Queue = asyncio.Queue()
    calls = {"turn": 0, "verify": 0}

    async def fake_chat(messages, tools=None, stream=True,
                        model="", effort=None, max_tokens=None):
        if not stream:
            # tool-less completion verifier
            calls["verify"] += 1
            content = await verdicts.get()
            return {"choices": [{"message": {"content": content}}]}
        calls["turn"] += 1
        events = await turn_scripts.get()
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    # Turn 1: model calls a real tool; Turn 2/3: model finishes (no tools).
    await turn_scripts.put([
        {"type": "tool_calls", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "bash",
                          "arguments": json.dumps({"command": "echo work"})}},
        ]},
    ])
    await turn_scripts.put([{"type": "content", "text": "Done the work"},
                            {"type": "finish"}])
    await turn_scripts.put([{"type": "content", "text": "Finished"},
                            {"type": "finish"}])

    # Verifier: fail once (with a nextAction), then pass.
    await verdicts.put(json.dumps({
        "passed": False, "reason": "no evidence yet",
        "nextAction": "write the file",
    }))
    await verdicts.put(json.dumps({
        "passed": True, "reason": "evidence present", "nextAction": "",
    }))

    events = await collect(loop.run_agent(cid, "Fix bug A", ws))
    types = [e["type"] for e in events]

    # done emitted exactly once, AFTER the verification_failed goal.
    assert types.count("done") == 1
    gi, di = types.index("goal"), types.index("done")
    assert events[gi]["phase"] == "verification_failed"
    assert gi < di
    # 3 turn calls (tool, finish, finish) + 2 verifier calls (fail, pass).
    assert calls["turn"] == 3
    assert calls["verify"] == 2

    goal = next(e for e in events if e["type"] == "goal")
    assert goal["passed"] is False
    assert goal["nextAction"] == "write the file"


@pytest.mark.asyncio
async def test_verifier_pass_first_try_emits_no_goal(tmp_path, monkeypatch):
    """When the verifier passes on the first audit, no `goal` event fires and
    the turn completes directly — the common case stays cheap."""
    from backend.db.database import create_conversation

    cid = await create_conversation("verify ok")
    ws = str(tmp_path)

    turn_scripts: asyncio.Queue = asyncio.Queue()
    verdicts: asyncio.Queue = asyncio.Queue()
    calls = {"turn": 0, "verify": 0}

    async def fake_chat(messages, tools=None, stream=True,
                        model="", effort=None, max_tokens=None):
        if not stream:
            calls["verify"] += 1
            content = await verdicts.get()
            return {"choices": [{"message": {"content": content}}]}
        calls["turn"] += 1
        return FakeStream(await turn_scripts.get())

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    await turn_scripts.put([{"type": "content", "text": "All done"},
                            {"type": "finish"}])
    await verdicts.put(json.dumps({
        "passed": True, "reason": "done", "nextAction": "",
    }))

    events = await collect(loop.run_agent(cid, "do the thing", ws))
    types = [e["type"] for e in events]
    assert types.count("goal") == 0
    assert types.count("done") == 1
    assert calls["verify"] == 1


@pytest.mark.asyncio
async def test_verifier_infra_error_fails_open(tmp_path, monkeypatch):
    """A verifier infra error (provider failure) must NOT wedge the turn —
    it fails open and the turn completes."""
    from backend.db.database import create_conversation

    cid = await create_conversation("verify flaky")
    ws = str(tmp_path)

    turn_scripts: asyncio.Queue = asyncio.Queue()
    chat_calls = {"n": 0}

    async def fake_chat(messages, tools=None, stream=True,
                        model="", effort=None, max_tokens=None):
        chat_calls["n"] += 1
        if not stream:
            # verifier: raise like a provider failure — verify() must catch
            # it and fail open.
            from backend.agent import model_client
            raise model_client.ModelError("500 server error")
        return FakeStream(await turn_scripts.get())

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    await turn_scripts.put([{"type": "content", "text": "Done"},
                            {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "finish it", ws))
    types = [e["type"] for e in events]
    assert types.count("done") == 1
    assert "goal" not in types          # no failed-iteration gate
    assert chat_calls["n"] == 2         # one turn + one (failed) verifier


@pytest.mark.asyncio
async def test_verifier_disabled_never_calls_verifier(tmp_path, monkeypatch):
    """completion_verifier=False restores the old behavior: the turn ends on
    the model's own stop, with no verifier call and no goal event."""
    from backend.db.database import create_conversation

    cid = await create_conversation("no verify")
    ws = str(tmp_path)

    turn_scripts: asyncio.Queue = asyncio.Queue()
    calls = {"turn": 0, "verify": 0}

    async def fake_chat(messages, tools=None, stream=True,
                        model="", effort=None, max_tokens=None):
        if not stream:
            calls["verify"] += 1
            return {"choices": [{"message": {"content": '{"passed": false}'}}]}
        calls["turn"] += 1
        return FakeStream(await turn_scripts.get())

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    # Disable via config (DEFAULTS + the override).
    def disabled_cfg(**_):
        cfg = dict(cfgmod.DEFAULTS)
        cfg["completion_verifier"] = False
        return cfg
    monkeypatch.setattr(loop, "load_config", disabled_cfg)

    await turn_scripts.put([{"type": "content", "text": "Done"},
                            {"type": "finish"}])
    events = await collect(loop.run_agent(cid, "go", ws))
    types = [e["type"] for e in events]
    assert types.count("done") == 1
    assert "goal" not in types
    assert calls["verify"] == 0          # verifier never invoked
