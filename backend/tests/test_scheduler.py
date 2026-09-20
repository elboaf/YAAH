"""Scheduled agents (issue #41): sanitize/schedule math, fire mechanics,
catch-up, and the sandbox-only approval policy end-to-end in the loop."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from backend.agent import loop
from backend.agent import scheduler as sched


# ---------------------------------------------------------------- sanitize


def test_sanitize_defaults_and_bounds():
    a = sched.sanitize_agent({"name": "  ", "conversation_id": "7", "kind": "nope"})
    assert a["name"] == "Agent"
    assert a["conversation_id"] == 7
    assert a["kind"] == "interval"
    assert a["policy"] == "sandbox-only"  # the default
    assert a["enabled"] is True
    assert a["id"]
    # interval clamped to the sanity bounds
    lo = sched.sanitize_agent({"interval_minutes": 0})
    hi = sched.sanitize_agent({"interval_minutes": 10**9})
    assert lo["interval_minutes"] == sched.MIN_INTERVAL_MINUTES
    assert hi["interval_minutes"] == sched.MAX_INTERVAL_MINUTES
    # malformed wall time falls back, weekday clamps
    bad = sched.sanitize_agent({"time": "25:99", "weekday": 11})
    assert bad["time"] == "09:00"
    assert bad["weekday"] == 6


# ---------------------------------------------------------------- next-run math


def test_next_run_interval_counts_from_now():
    now = datetime(2026, 9, 20, 12, 0)
    a = sched.sanitize_agent({"kind": "interval", "interval_minutes": 30})
    assert sched.compute_next_run(a, now) == now + timedelta(minutes=30)


def test_next_run_daily_rolls_to_tomorrow_when_passed():
    now = datetime(2026, 9, 20, 12, 0)  # Sunday
    a = sched.sanitize_agent({"kind": "daily", "time": "09:00"})
    nxt = sched.compute_next_run(a, now)
    assert nxt == datetime(2026, 9, 21, 9, 0)


def test_next_run_weekly_targets_weekday_and_time():
    now = datetime(2026, 9, 20, 12, 0)  # Sunday, weekday index 6
    a = sched.sanitize_agent({"kind": "weekly", "time": "09:00", "weekday": 6})
    # Same day but 09:00 already passed -> a full week later.
    assert sched.compute_next_run(a, now) == datetime(2026, 9, 27, 9, 0)
    a2 = sched.sanitize_agent({"kind": "weekly", "time": "23:00", "weekday": 6})
    # Later today is still ahead -> today.
    assert sched.compute_next_run(a2, now) == datetime(2026, 9, 20, 23, 0)


# ---------------------------------------------------------------- upsert/remove


def test_upsert_and_remove_roundtrip(monkeypatch, tmp_path):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    a = sched.upsert_agent({"name": "watcher", "prompt": "check builds", "conversation_id": 3})
    assert a["next_run_at"]
    assert len(sched.get_agents()) == 1
    # update by id: schedule fields changed -> clock restarts
    b = sched.upsert_agent({**a, "interval_minutes": 15})
    assert len(sched.get_agents()) == 1
    assert b["interval_minutes"] == 15
    # non-schedule edits keep the old next_run_at
    c = sched.upsert_agent({**b, "prompt": "new prompt"})
    assert c["next_run_at"] == b["next_run_at"]
    assert sched.remove_agent(a["id"]) is True
    assert sched.remove_agent(a["id"]) is False
    assert sched.get_agents() == []


# ---------------------------------------------------------------- firing


@pytest.mark.asyncio
async def test_fire_agent_runs_and_advances(monkeypatch, tmp_path):
    from backend.agent import config as cfgmod
    from backend.db.database import create_conversation

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    fired = {}

    async def fake_drain(cid, prompt, workspace, policy):
        fired.update(cid=cid, prompt=prompt, policy=policy)

    monkeypatch.setattr(sched, "_drain_run", fake_drain)

    cid = await create_conversation("agent chat")
    a = sched.upsert_agent({
        "name": "watcher", "prompt": "check builds",
        "conversation_id": cid, "next_run_at": "2020-01-01T00:00:00",
    })
    assert await sched.fire_agent(a) is True
    await asyncio.sleep(0)  # let fire_agent's create_task(_drain_run) run
    assert fired["cid"] == cid
    assert fired["policy"] == "sandbox-only"
    # The scheduler-provided marker is in the prompt and the schedule advanced.
    assert "watcher" in fired["prompt"]
    saved = sched.get_agents()[0]
    assert saved["last_run_at"]
    assert datetime.fromisoformat(saved["next_run_at"]) > datetime.now()


@pytest.mark.asyncio
async def test_fire_agent_busy_postpones_without_losing_run(monkeypatch, tmp_path):
    from backend.agent import config as cfgmod, loop as loop_mod
    from backend.db.database import create_conversation

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    cid = await create_conversation("busy chat")
    a = sched.upsert_agent({"name": "w", "prompt": "p", "conversation_id": cid})
    # Claim the conversation like a live turn would.
    assert loop_mod.try_begin_run(cid)
    try:
        assert await sched.fire_agent(a) is False
        saved = sched.get_agents()[0]
        # Postponed ~BUSY_RETRY_SECONDS out; last_run untouched.
        nxt = datetime.fromisoformat(saved["next_run_at"])
        assert timedelta(0) < nxt - datetime.now() <= timedelta(seconds=sched.BUSY_RETRY_SECONDS + 5)
        assert saved["last_run_at"] == ""
    finally:
        loop_mod._running_convs.discard(cid)


@pytest.mark.asyncio
async def test_fire_agent_missing_conversation_disables(monkeypatch, tmp_path):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    a = sched.upsert_agent({"name": "w", "prompt": "p", "conversation_id": 424242})
    assert await sched.fire_agent(a) is False
    assert sched.get_agents()[0]["enabled"] is False


@pytest.mark.asyncio
async def test_startup_catchup_fires_only_stale(monkeypatch, tmp_path):
    from backend.agent import config as cfgmod
    from backend.db.database import create_conversation

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    fired = []
    monkeypatch.setattr(
        sched, "fire_agent",
        lambda a, advance_schedule=True: fired.append(a["id"]) or asyncio.sleep(0, result=True),
    )
    cid = await create_conversation("c1")
    stale = sched.upsert_agent({
        "name": "stale", "prompt": "p", "conversation_id": cid,
        "next_run_at": "2020-01-01T00:00:00",
    })
    future = sched.upsert_agent({
        "name": "future", "prompt": "p", "conversation_id": cid,
        "next_run_at": (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds"),
    })
    paused = sched.upsert_agent({
        "name": "paused", "prompt": "p", "conversation_id": cid, "enabled": False,
        "next_run_at": "2020-01-01T00:00:00",
    })
    await sched.startup_catchup()
    assert fired == [stale["id"]]
    assert future["id"] not in fired and paused["id"] not in fired


# ---------------------------------------------------------------- sandbox-only policy e2e


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
    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


async def _collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


@pytest.mark.asyncio
async def test_sandbox_only_run_skips_gated_tools_without_asking(
    fake_model, tmp_path, monkeypatch
):
    """The whole point of the default policy: a scheduled run attempts a
    write, gets the skip note instead of an approval card, and continues."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cfgmod, "DEFAULTS", {**cfgmod.DEFAULTS, "access_mode": "ask"})
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    from backend.db.database import create_conversation

    cid = await create_conversation("t-sandbox-only")
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

    events = await _collect(
        loop.run_agent(cid, "go", str(tmp_path), policy="sandbox-only")
    )
    types = [e["type"] for e in events]
    # No approval round-trip at all — the gate never blocks an unattended run.
    assert "approval_request" not in types
    assert "approval_decision" not in types
    result = next(e for e in events if e["type"] == "tool_result")
    assert "sandbox-only" in json.dumps(result["result"])
    assert not (tmp_path / "a.txt").exists()
    # The turn continued past the skip to its final answer.
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_sandbox_only_ask_user_answers_itself(fake_model, tmp_path, monkeypatch):
    """ask_user in an unattended run must not wedge the turn: it returns a
    note immediately and the loop carries on."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cfgmod, "DEFAULTS", {**cfgmod.DEFAULTS, "access_mode": "ask"})
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    from backend.db.database import create_conversation

    cid = await create_conversation("t-sandbox-only-ask")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "q1",
                "type": "function",
                "function": {"name": "ask_user", "arguments": json.dumps({"question": "hi?"})},
            }],
        },
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await _collect(
        loop.run_agent(cid, "go", str(tmp_path), policy="sandbox-only")
    )
    assert not any(loop._pending_answers.get(f"{cid}:q1") is not None for _ in [0])
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["result"]["answer"] is None
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_policy_full_overrides_global_ask(fake_model, tmp_path, monkeypatch):
    """policy="full" runs a gated tool even though the global mode is ask."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cfgmod, "DEFAULTS", {**cfgmod.DEFAULTS, "access_mode": "ask"})
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    from backend.db.database import create_conversation

    cid = await create_conversation("t-policy-full")
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

    events = await _collect(loop.run_agent(cid, "go", str(tmp_path), policy="full"))
    types = [e["type"] for e in events]
    assert "approval_request" not in types
    assert (tmp_path / "a.txt").exists()
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_sandbox_only_note_in_system_prompt(fake_model, tmp_path, monkeypatch):
    """The system prompt tells the model up front what is off-limits."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    seen_prompts = []

    async def spy_chat(messages, tools=None, stream=True):
        seen_prompts.append(messages[0]["content"])
        return FakeStream([{"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", spy_chat)
    from backend.db.database import create_conversation

    cid = await create_conversation("t-note")
    await _collect(loop.run_agent(cid, "go", str(tmp_path), policy="sandbox-only"))
    assert "sandbox-only policy" in seen_prompts[0]
