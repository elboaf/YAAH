"""Issue #445 - per-tool-call durations, measured once at the source.

Server-side timing: the agent loop stamps time.monotonic() around each tool
execution and carries duration_ms on the tool_result event and the persisted
tool row, so the post-run trace, sub-agent traces, and reloaded history all
show real durations. Client startedAt/finishedAt stays the live fallback.

Contract:
- completed parent tool calls: duration_ms int >= 0 on the event AND the row;
- malformed-arguments paths never started a timer: no duration, no fake 0;
- sub-agent nested tool calls carry duration_ms in the transcript snapshot;
- spawn_agent rows carry the whole batch-wait-plus-run wall time;
- model-context replay (get_history) omits durations (prompt-shape freeze);
- remote commits round-trip duration_ms into the owner's rows.
"""
import json

import aiosqlite
import pytest

import backend.db.database as database
from backend.agent import loop
from backend.db.database import (
    acquire_remote_edit_lease,
    commit_remote_conversation,
    create_conversation,
    get_messages,
)


async def _make_pre_445_db(path):
    """Write a pre-#445 database: today's schema minus the duration_ms
    column (which is exactly what an upgraded install carries), with the
    messages revision trigger dropped first (the trigger's UPDATE OF list
    names the column, and DROP COLUMN refuses a referenced column)."""
    async with aiosqlite.connect(path) as db:
        await db.executescript(database.SCHEMA)
        await db.execute(
            "DROP TRIGGER IF EXISTS messages_remote_revision_update"
        )
        await db.execute("ALTER TABLE messages DROP COLUMN duration_ms")
        await db.execute(
            "INSERT INTO conversations (title, workspace) VALUES ('pre-upgrade', NULL)"
        )
        await db.commit()


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


async def collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


def _bash_call(call_id="c1", command="echo tool ran"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "bash", "arguments": json.dumps({"command": command})},
    }


@pytest.mark.asyncio
async def test_tool_result_event_and_row_carry_server_duration(
    fake_model, tmp_path
):
    """AC: a completed parent tool call carries duration_ms on the result
    event and the persisted row (source-measured, gate wait included)."""
    cid = await create_conversation("durations")
    fake_model.append([{"type": "tool_calls", "tool_calls": [_bash_call()]}])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    tr = next(e for e in events if e["type"] == "tool_result")
    assert isinstance(tr.get("duration_ms"), int), tr
    assert tr["duration_ms"] >= 0

    rows = await get_messages(cid)
    row = next(r for r in rows if r["role"] == "tool")
    assert row["duration_ms"] == tr["duration_ms"]


@pytest.mark.asyncio
async def test_malformed_args_result_has_no_duration(fake_model, tmp_path):
    """A call whose arguments never parsed never started a timer: the
    tool_result carries no duration and the row stays NULL - an open-ended
    render, never a fake 0.0s."""
    cid = await create_conversation("bad args")
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "bash", "arguments": "{not json"},
    }
    fake_model.append([{"type": "tool_calls", "tool_calls": [call]}])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    tr = next(e for e in events if e["type"] == "tool_result")
    assert "duration_ms" not in tr
    assert "error" in tr["result"]

    rows = await get_messages(cid)
    row = next(r for r in rows if r["role"] == "tool")
    assert row["duration_ms"] is None


@pytest.mark.asyncio
async def test_spawn_row_and_sub_agent_children_carry_durations(
    fake_model, tmp_path
):
    """AC: sub-agent tool calls show durations - the spawn row carries the
    batch's wall time and the transcript snapshot stamps every nested call."""
    cid = await create_conversation("spawn durations")
    fake_model.append([
        {
            "type": "tool_calls",
            "tool_calls": [{
                "id": "s1",
                "type": "function",
                "function": {
                    "name": "spawn_agent",
                    "arguments": json.dumps({
                        "agent_type": "general-purpose",
                        "prompt": "Run the echo.",
                    }),
                },
            }],
        },
    ])
    # The sub-agent's turn: run bash, then answer.
    fake_model.append([
        {"type": "tool_calls", "tool_calls": [_bash_call("sub-c1")]},
        {"type": "finish"},
    ])
    fake_model.append([
        {"type": "content", "text": "sub done"},
        {"type": "finish"},
    ])
    fake_model.append([{"type": "content", "text": "parent done"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    spawn_tr = next(
        e for e in events
        if e["type"] == "tool_result" and e.get("name") == "spawn_agent"
    )
    assert isinstance(spawn_tr.get("duration_ms"), int), spawn_tr

    rows = await get_messages(cid)
    spawn_row = next(
        r for r in rows
        if r["role"] == "tool" and (r["tool_calls"] or [{}])[0].get("name") == "spawn_agent"
    )
    assert spawn_row["duration_ms"] == spawn_tr["duration_ms"]

    # Nested (child) calls are timed inside the sub's transcript snapshot.
    entries = spawn_row["sub_agent_transcript"]["transcript"]
    child = next(e for e in entries if e.get("role") == "tool")
    assert isinstance(child.get("duration_ms"), int), child


@pytest.mark.asyncio
async def test_history_replay_omits_durations(fake_model, tmp_path):
    """The prompt-shape freeze: get_history must not leak duration_ms into
    replayed tool rows (strict provider templates reject unknown fields)."""
    cid = await create_conversation("replay shape")
    fake_model.append([{"type": "tool_calls", "tool_calls": [_bash_call()]}])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])
    await collect(loop.run_agent(cid, "go", str(tmp_path)))

    history = await loop.load_history(cid)
    tool_msgs = [m for m in history if m["role"] == "tool"]
    assert tool_msgs, "the tool result must replay"
    for m in tool_msgs:
        assert "duration_ms" not in m


@pytest.mark.asyncio
async def test_commit_remote_conversation_persists_duration():
    """AC: durations survive a reload on remote conversations - the commit
    ingest round-trips duration_ms into the owner's rows."""
    cid = await create_conversation("remote ingest")
    lease = await acquire_remote_edit_lease(cid, "r:1")
    await commit_remote_conversation(
        cid,
        conversation={"title": "remote ingest", "workspace": "C:/w"},
        messages=[
            {"id": 1, "role": "user", "content": "go"},
            {
                "id": 2, "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "r1", "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }],
            },
            {
                "id": 3, "role": "tool", "content": "{}",
                "tool_call_id": "r1", "duration_ms": 1234,
            },
        ],
        commit_id="c-445",
        lease_token=lease["lease_token"],
        revision=lease["revision"],
    )
    rows = await get_messages(cid)
    tool_row = next(r for r in rows if r["role"] == "tool")
    assert tool_row["duration_ms"] == 1234


@pytest.mark.asyncio
async def test_run_sub_agent_transcript_stamps_durations(monkeypatch, tmp_path):
    """Unit level: run_sub_agent's own snapshot records duration_ms per
    nested tool call (the transcript is what reloads render)."""
    import backend.agent.model_client as model_client
    from backend.agent import subagents

    scripts = [
        [
            {"type": "tool_calls", "tool_calls": [_bash_call("u1")]},
            {"type": "finish"},
        ],
        [
            {"type": "content", "text": "unit done"},
            {"type": "finish"},
        ],
    ]

    async def fake_chat(messages, tools=None, stream=True):
        return FakeStream(scripts.pop(0))

    monkeypatch.setattr(model_client, "chat", fake_chat)

    result = await subagents.run_sub_agent(
        subagents.get_agent_def("general-purpose"),
        "run the echo",
        str(tmp_path),
    )
    tool_entries = [e for e in result["transcript"] if e.get("role") == "tool"]
    assert tool_entries, "the nested call must be in the transcript"
    assert isinstance(tool_entries[0].get("duration_ms"), int)


@pytest.mark.asyncio
async def test_malformed_then_valid_call_ordering(fake_model, tmp_path):
    """Regression: a malformed-args call AFTER a valid one in the same
    turn must not crash the loop (stale/None timer) nor inherit the
    previous call's duration."""
    # Valid call FIRST, malformed second: the stale-timer trap. After the
    # valid call the timer slot is None; an existence-guard ("_t0" in
    # dir()) would still try monotonic() - None and blow up the turn.
    cid = await create_conversation("bad second")
    bad = {
        "id": "b1", "type": "function",
        "function": {"name": "bash", "arguments": "{oops"},
    }
    fake_model.append([{"type": "tool_calls", "tool_calls": [_bash_call("g1"), bad]}])
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    trs = {e["call_id"]: e for e in events if e["type"] == "tool_result"}
    assert set(trs) == {"b1", "g1"}
    assert isinstance(trs["g1"].get("duration_ms"), int)
    assert "duration_ms" not in trs["b1"]

    rows = await get_messages(cid)
    dur = {r["tool_call_id"]: r["duration_ms"] for r in rows if r["role"] == "tool"}
    assert isinstance(dur["g1"], int), "valid call keeps its own duration"
    assert dur["b1"] is None, "malformed call must not carry a duration"


@pytest.mark.asyncio
async def test_sub_agent_malformed_first_call_survives(monkeypatch, tmp_path):
    """Regression: the sub-agent's malformed-args call (first in its run)
    must produce an error result row with no duration - not kill the run
    with an unbound duration_ms (NameError -> status=error)."""
    import backend.agent.model_client as model_client
    from backend.agent import subagents

    scripts = [
        [
            {"type": "tool_calls", "tool_calls": [{
                "id": "u0", "type": "function",
                "function": {"name": "bash", "arguments": "{nope"},
            }]},
            {"type": "finish"},
        ],
        [{"type": "content", "text": "recovered"}, {"type": "finish"}],
    ]

    async def fake_chat(messages, tools=None, stream=True):
        return FakeStream(scripts.pop(0))

    monkeypatch.setattr(model_client, "chat", fake_chat)
    result = await subagents.run_sub_agent(
        subagents.get_agent_def("general-purpose"),
        "call bash with broken args, then recover",
        str(tmp_path),
    )
    assert result["status"] == "completed", result
    tool_entries = [e for e in result["transcript"] if e.get("role") == "tool"]
    assert tool_entries, "the malformed call must still be recorded"
    assert "error" in json.loads(tool_entries[0]["content"])
    assert tool_entries[0].get("duration_ms") is None


@pytest.mark.asyncio
async def test_upgraded_db_receives_duration_ms_trigger(monkeypatch, tmp_path):
    """The messages revision trigger's UPDATE OF list is code-owned (the
    same #383 contract for the conversations trigger). An upgraded
    database must pick up the new body - CREATE TRIGGER IF NOT EXISTS
    would leave the pre-#445 body in place, so duration_ms writes would
    never bump the revision counter."""
    old_db = tmp_path / "upgraded.db"
    await _make_pre_445_db(old_db)
    monkeypatch.setattr(database, "DB_PATH", old_db)
    db = await database.get_db()
    try:
        cur = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
            " AND name = 'messages_remote_revision_update'"
        )
        trig = await cur.fetchone()
        assert "duration_ms" in (trig["sql"] or ""), trig["sql"]

        cur = await db.execute(
            "INSERT INTO messages (conversation_id, role, content)"
            " VALUES (1, 'tool', 'done')"
        )
        mid = cur.lastrowid
        cur = await db.execute(
            "SELECT remote_revision_counter FROM conversations WHERE id = 1"
        )
        before = (await cur.fetchone())["remote_revision_counter"]
        await db.execute(
            f"UPDATE messages SET duration_ms = 42 WHERE id = {mid}"
        )
        await db.commit()
        cur = await db.execute(
            "SELECT remote_revision_counter FROM conversations WHERE id = 1"
        )
        after = (await cur.fetchone())["remote_revision_counter"]
        assert after == before + 1
    finally:
        await db.close()
