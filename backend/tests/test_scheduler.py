"""Scheduled agents (issue #41) — tests follow the issue's task checklist:
scheduler due computation (interval + daily/weekly), skip-missed-fires,
fire pipeline (effective prompt, memory toggle, model/effort), policy
interception (sandbox-only skip, autonomous auto-approve), global retry
with backoff, standing instructions, retention trim."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from backend.agent import loop
from backend.agent import scheduler as sched
from backend.agent.scheduler import get_retry_settings, save_retry_settings
from backend.db.database import (
    add_instruction,
    add_message,
    create_agent,
    create_conversation,
    get_agent,
    get_messages,
    list_agents,
    list_instructions,
    trim_agent_transcript,
    update_agent,
)


def make_agent(workspace="C:/ws", name="nightly", prompt="summarize commits", **kw):
    fields = {
        "id": kw.pop("id", None) or uuid_hex(),
        "workspace": workspace,
        "name": name,
        "prompt": prompt,
        "schedule_type": "interval",
        "schedule_spec": json.dumps({"minutes": 30}),
        "approval_policy": "sandbox-only",
        "enabled": 1,
        "memory_enabled": 1,
    }
    fields.update(kw)
    return fields


def uuid_hex():
    import uuid

    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------- schedule math


def test_interval_next_fire_is_strictly_after_now():
    now = datetime(2026, 9, 20, 12, 0)
    nxt = sched.compute_next_fire("interval", json.dumps({"minutes": 90}), after=now)
    assert nxt == now + timedelta(minutes=90)
    assert nxt > now  # never immediately on save


def test_daily_next_fire_rolls_to_tomorrow_when_passed():
    now = datetime(2026, 9, 20, 12, 0)  # Sunday
    nxt = sched.compute_next_fire("daily", json.dumps({"time": "09:00"}), after=now)
    assert nxt == datetime(2026, 9, 21, 9, 0)


def test_weekly_next_fire_targets_weekday_and_time():
    now = datetime(2026, 9, 20, 12, 0)  # Sunday = weekday 6
    nxt = sched.compute_next_fire(
        "weekly", json.dumps({"weekday": 6, "time": "09:00"}), after=now
    )
    # Same weekday, but 09:00 already passed -> a full week later.
    assert nxt == datetime(2026, 9, 27, 9, 0)
    nxt2 = sched.compute_next_fire(
        "weekly", json.dumps({"weekday": 6, "time": "23:00"}), after=now
    )
    assert nxt2 == datetime(2026, 9, 20, 23, 0)  # later today


def test_normalize_schedule_sanitizes_garbage():
    stype, spec = sched.normalize_schedule("cron", {"expr": "* * * *"})
    assert stype == "interval"
    assert json.loads(spec)["minutes"] == 60  # falls back, never crashes
    stype, spec = sched.normalize_schedule("weekly", {"weekday": 9, "time": "bad"})
    parsed = json.loads(spec)
    assert parsed["weekday"] == 6
    assert parsed["time"] == "09:00"


def test_describe_schedule_is_human():
    assert sched.describe_schedule("interval", json.dumps({"minutes": 120})) == "every 2 hours"
    assert sched.describe_schedule("daily", json.dumps({"time": "09:00"})) == "daily at 09:00"
    assert sched.describe_schedule("weekly", json.dumps({"weekday": 0, "time": "08:15"})) == "weekly Mon at 08:15"


# ------------------------------------------------------------- global retry


def test_global_retry_settings_roundtrip(monkeypatch, tmp_path):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert get_retry_settings() == (2, 5)  # defaults
    save_retry_settings(4, 15)
    assert get_retry_settings() == (4, 15)
    save_retry_settings(99, 0)  # clamped
    assert get_retry_settings() == (10, 1)


# ------------------------------------------------------------- skip-missed


@pytest.mark.asyncio
async def test_missed_fires_skip_silently_on_startup():
    """Issue spec: missed fires while YAAH is closed are skipped — the
    overdue slot rolls forward and NO catch-up run happens."""
    conv = await create_conversation("agent chat", chat_type="agent")
    overdue = (datetime.now() - timedelta(hours=6)).isoformat(timespec="seconds")
    agent_row = await create_agent(make_agent(
        conversation_id=conv, next_fire_at=overdue,
    ))
    fired = []

    async def fake_run(*a, **kw):
        fired.append(a)
        return
        yield  # pragma: no cover — async generator formality

    import backend.agent.loop as loop_mod

    original = loop_mod.run_agent
    loop_mod.run_agent = fake_run
    try:
        await sched.startup_roll_forward()
        await asyncio.sleep(0.05)
    finally:
        loop_mod.run_agent = original
    assert fired == []  # no catch-up fire
    agent = await get_agent(agent_row["id"])
    # The overdue slot rolled forward into the future.
    assert datetime.fromisoformat(agent["next_fire_at"]) > datetime.now()


# ------------------------------------------------------------- fire pipeline


async def drain_pending(max_wait=1.0):
    for _ in range(int(max_wait / 0.01)):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_fire_pipeline_prompt_memory_and_overrides(monkeypatch):
    """Effective prompt = user prompt + standing instructions verbatim; the
    memory toggle and per-agent model/effort reach run_agent untouched."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        conversation_id=conv,
        prompt="summarize commits",
        memory_enabled=0,
        model="gpt-x",
        effort="high",
        retention=0,
    ))
    await add_instruction(agent_row["id"], "ignore draft PRs")
    await add_instruction(agent_row["id"], "keep it under 10 lines")

    captured = {}

    async def fake_run(cid, prompt, workspace, *, policy, include_history,
                       model_override, effort_override):
        captured.update(
            cid=cid, prompt=prompt, policy=policy, include_history=include_history,
            model=model_override, effort=effort_override,
        )
        return
        yield  # pragma: no cover

    monkeypatch.setattr(loop, "run_agent", fake_run)
    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent) == "started"
    await drain_pending()

    assert captured["cid"] == conv
    assert captured["prompt"].startswith("summarize commits")
    assert "# Standing instructions" in captured["prompt"]
    assert "- ignore draft PRs" in captured["prompt"]
    assert "- keep it under 10 lines" in captured["prompt"]
    assert captured["include_history"] is False  # memory toggle off
    assert captured["model"] == "gpt-x"
    assert captured["effort"] == "high"
    assert captured["policy"] == "sandbox-only"
    agent = await get_agent(agent_row["id"])
    assert agent["last_status"] == "ok"
    # Regular schedule advanced from fire time, never immediate.
    assert datetime.fromisoformat(agent["next_fire_at"]) > datetime.now()


@pytest.mark.asyncio
async def test_fire_failure_schedules_global_backoff_retry(monkeypatch):
    """A failed fire re-enters through the due tick at now+backoff, up to
    the global retry_count; the regular slot isn't pushed out by retries."""
    from backend.agent import config as cfgmod

    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        conversation_id=conv,
        next_fire_at=datetime.now().isoformat(timespec="seconds"),
    ))

    calls = {"n": 0}

    async def fake_run(cid, prompt, workspace, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider 502")
        return
        yield  # pragma: no cover

    monkeypatch.setattr(loop, "run_agent", fake_run)
    monkeypatch.setattr(sched, "get_retry_settings", lambda: (2, 5))

    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent) == "started"
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "error"
    # Retry parked ~backoff minutes out, regular slot preserved.
    retry_at = datetime.fromisoformat(row["next_fire_at"])
    delta = retry_at - datetime.now()
    assert timedelta(0) < delta <= timedelta(minutes=5, seconds=30)

    # The retry fire (via the due tick) succeeds -> settles ok, state cleared.
    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent, is_retry=True) == "started"
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "ok"
    assert agent["id"] not in sched._retry_state


@pytest.mark.asyncio
async def test_retry_exhaustion_leaves_error_and_clears_state(monkeypatch):
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(conversation_id=conv))

    async def always_fail(cid, prompt, workspace, **kw):
        raise RuntimeError("down")
        yield  # pragma: no cover

    monkeypatch.setattr(loop, "run_agent", always_fail)
    monkeypatch.setattr(sched, "get_retry_settings", lambda: (1, 5))

    agent = await get_agent(agent_row["id"])
    await sched.fire_agent(agent)  # initial attempt fails
    await drain_pending()
    agent = await get_agent(agent_row["id"])
    await sched.fire_agent(agent, is_retry=True)  # retry 1 of 1 fails
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "error"
    assert agent["id"] not in sched._retry_state  # retries exhausted


@pytest.mark.asyncio
async def test_retention_trim_keeps_last_n_runs():
    conv = await create_conversation("agent chat", chat_type="agent")
    for i in range(5):
        await add_message(conv, "user", f"run {i}")
        await add_message(conv, "assistant", f"answer {i}")
    await trim_agent_transcript(conv, 2)
    rows = await get_messages(conv)
    texts = [r["content"] for r in rows]
    assert texts == ["run 3", "answer 3", "run 4", "answer 4"]


@pytest.mark.asyncio
async def test_due_requires_enabled_and_prompt():
    now = datetime.now()
    past = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    off = make_agent(id=uuid_hex(), enabled=0, next_fire_at=past)
    noprompt = make_agent(id=uuid_hex(), prompt="  ", next_fire_at=past)
    assert not sched._is_due(off, now)
    assert not sched._is_due(noprompt, now)
    live = make_agent(id=uuid_hex(), next_fire_at=past)
    assert sched._is_due(live, now)


# ------------------------------------------------- policy interception (e2e)


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

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        scripts_last = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(scripts_last)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


async def _collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


@pytest.mark.asyncio
async def test_sandbox_only_skips_with_contract_text(fake_model, tmp_path, monkeypatch):
    """Spec: approval-required tools are skipped, the tool result says
    'skipped: approval required', and the run CONTINUES."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cfgmod, "DEFAULTS", {**cfgmod.DEFAULTS, "access_mode": "ask"})
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    cid = await create_conversation("t")
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "w1", "type": "function",
            "function": {"name": "write_file",
                         "arguments": json.dumps({"path": "a.txt", "content": "hi"})},
        }]},
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await _collect(loop.run_agent(cid, "go", str(tmp_path), policy="sandbox-only"))
    types = [e["type"] for e in events]
    assert "approval_request" not in types  # an unattended run never gates
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["result"]["error"].startswith("skipped: approval required")
    assert not (tmp_path / "a.txt").exists()
    assert events[-1]["type"] == "done"  # run continued past the skip


@pytest.mark.asyncio
async def test_autonomous_auto_approves_despite_global_ask(fake_model, tmp_path, monkeypatch):
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cfgmod, "DEFAULTS", {**cfgmod.DEFAULTS, "access_mode": "ask"})
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    cid = await create_conversation("t")
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [{
            "id": "w1", "type": "function",
            "function": {"name": "write_file",
                         "arguments": json.dumps({"path": "a.txt", "content": "hi"})},
        }]},
    ])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await _collect(loop.run_agent(cid, "go", str(tmp_path), policy="autonomous"))
    assert "approval_request" not in [e["type"] for e in events]
    assert (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_memory_toggle_off_builds_fresh_context(fake_model, tmp_path, monkeypatch):
    """memory_enabled=False: the system prompt stands alone — prior
    transcript rows are NOT replayed into model context."""
    from backend.agent import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loop, "load_config", cfgmod.load_config)

    cid = await create_conversation("t")
    await add_message(cid, "user", "earlier conversation")
    await add_message(cid, "assistant", "earlier answer")

    seen = {}

    async def spy_chat(messages, tools=None, stream=True, model="", effort=""):
        seen["roles"] = [m["role"] for m in messages]
        seen["user_texts"] = [m.get("content") for m in messages if m["role"] == "user"]
        return FakeStream([{"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", spy_chat)
    await _collect(loop.run_agent(cid, "fresh fire", str(tmp_path), include_history=False))
    assert seen["roles"] == ["system", "user"]
    assert "earlier conversation" not in seen["user_texts"]


@pytest.mark.asyncio
async def test_stop_kills_inflight_tool_of_scheduled_run(fake_model, tmp_path, monkeypatch):
    """Stop must not wait out a long-running tool: cancelling mid-tool kills
    the tool task, the run settles at once, and the model gets no follow-up
    call. Regression for the row stop button looking like a no-op."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        workspace=str(tmp_path), conversation_id=conv, approval_policy="autonomous"))
    sleep_call = [{
        "type": "tool_calls",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": "sleep 60"})},
        }],
    }, {"type": "finish", "reason": "tool_calls"}]
    scripts = [sleep_call, [{"type": "content", "text": "never streamed"}, {"type": "finish"}]]

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        return FakeStream(scripts.pop(0) if scripts else [{"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    assert await sched.fire_agent(agent_row) == "started"
    await asyncio.sleep(2)  # the 60s sleep tool is now in flight
    t0 = asyncio.get_running_loop().time()
    loop.cancel_agent(conv)
    while (await get_agent(agent_row["id"]))["last_status"] == "running":
        await asyncio.sleep(0.1)
        assert asyncio.get_running_loop().time() - t0 < 10, "run did not stop after cancel"
    assert asyncio.get_running_loop().time() - t0 < 5
    # The cancelled turn must not continue to a follow-up model call.
    assert len(scripts) >= 1


@pytest.mark.asyncio
async def test_stop_or_overrun_does_not_refire_immediately(fake_model, tmp_path):
    """A run that outlives its schedule (busy-postpones roll next_fire_at
    ~60s ahead every tick) leaves the slot in the past at settle time.
    Settling must roll it forward to the next real slot — otherwise the
    next tick immediately re-fires the run the user just stopped."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        workspace=str(tmp_path), conversation_id=conv,
        schedule_spec=json.dumps({"minutes": 5})))

    hang = asyncio.Event()

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        await hang.wait()
        return FakeStream([{"type": "finish"}])

    loop.model_client.chat = fake_chat

    assert await sched.fire_agent(agent_row) == "started"
    while not loop.agent_is_running(conv):
        await asyncio.sleep(0.05)  # the model call is in flight
    # Simulate the long-run state: the slot kept being busy-postponed and
    # the user stops; at settle time the slot sits in the past.
    past = (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds")
    await update_agent(agent_row["id"], {"next_fire_at": past})
    hang.set()
    while (await get_agent(agent_row["id"]))["last_status"] == "running":
        await asyncio.sleep(0.05)

    row = await get_agent(agent_row["id"])
    nxt = datetime.fromisoformat(row["next_fire_at"])
    assert nxt > datetime.now(), "settled slot still in the past -> immediate refire"
    # And it rolled to a FULL interval from now, not a busy-postpone minute.
    assert nxt >= datetime.now() + timedelta(minutes=4)


@pytest.mark.asyncio
async def test_paused_agent_never_fires_and_run_now_refuses(fake_model, tmp_path):
    """Disabling an agent is a hard gate: the tick skips it and even an
    explicit run-now is refused instead of resurrecting it."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        workspace=str(tmp_path), conversation_id=conv, enabled=0))
    # Slot in the past: a paused agent must stay paused regardless.
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    await update_agent(agent_row["id"], {"next_fire_at": past})

    due = [a for a in await list_agents() if sched._is_due(a, datetime.now())]
    assert due == [], "tick fired a disabled agent"

    assert await sched.fire_agent(agent_row) == "disabled"


@pytest.mark.asyncio
async def test_pausing_mid_run_cancels_and_clears_retry(fake_model, tmp_path, monkeypatch):
    """Unchecking 'enabled' while a run is in flight stops that run and
    drops pending retries — the agent goes quiet immediately."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        workspace=str(tmp_path), conversation_id=conv))

    hang = asyncio.Event()

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        await hang.wait()
        return FakeStream([{"type": "finish"}])

    loop.model_client.chat = fake_chat
    assert await sched.fire_agent(agent_row) == "started"
    while not loop.agent_is_running(conv):
        await asyncio.sleep(0.05)

    # The edit path (main.py) calls these two on enabled -> disabled.
    assert loop.agent_is_running(conv)
    sched.cancel_agent_run(conv)
    sched.clear_retry_state(agent_row["id"])
    hang.set()
    while loop.agent_is_running(conv):
        await asyncio.sleep(0.05)
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "ok"
    assert agent_row["id"] not in sched._retry_state
