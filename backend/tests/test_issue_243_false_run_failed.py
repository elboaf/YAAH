"""Issue #243 — a completed run must never settle last_status="error".

The scheduler treats EVERY in-band `type: "error"` event as a failed fire,
but the loop emits non-fatal error events too: an unattended run whose
ask_user is auto-skipped emits the skip as tool results (fine), while a
claim refusal / busy conversation emits a turn-terminal error event with a
perfectly clean transcript — and "Run once now" then toasts "Agent run
failed" for a run that never even started its turn.

Contract (issue acceptance criteria):
- only FATAL error events settle "error" (loop marks them `fatal: true`,
  `kind: null`); non-fatal ones (`kind: "claim_refused"` / `"busy"`)
  settle "ok" and are logged instead;
- the settle path logs which source stamped last_status (AC 1);
- rate-limit quiet handling ("error_quiet") is untouched (AC 5).
"""
import json
from datetime import datetime

import pytest

from backend.agent import loop
from backend.agent import scheduler as sched
from backend.db.database import (
    RemoteProtocolError,
    create_agent,
    create_conversation,
    get_agent,
)
from backend.tests.test_scheduler import drain_pending, make_agent


@pytest.mark.asyncio
async def test_claim_refusal_settles_error(monkeypatch):
    """AC 3: a fire that can't claim the conversation (lease held / turn
    already running) IS a genuine failure — it settles 'error'."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        conversation_id=conv,
        next_fire_at=datetime.now().isoformat(timespec="seconds"),
    ))

    async def fake_run(cid, prompt, workspace, **kw):
        raise RemoteProtocolError("lease_held")
        yield  # pragma: no cover

    monkeypatch.setattr(loop, "run_agent", fake_run)
    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent) == "started"
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "error", "claim refusal is a genuine failure"


@pytest.mark.asyncio
async def test_unattended_skip_run_settles_ok(monkeypatch, caplog):
    """AC 2 + AC 4: an unattended run that completes — ask_user skipped with
    tool results, no error events — settles 'ok', never 'error'."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        conversation_id=conv,
        next_fire_at=datetime.now().isoformat(timespec="seconds"),
    ))

    async def fake_run(cid, prompt, workspace, **kw):
        yield json.dumps({"type": "content", "text": "did the thing"}) + "\n"
        yield json.dumps({"type": "finish"}) + "\n"

    monkeypatch.setattr(loop, "run_agent", fake_run)
    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent) == "started"
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "ok"


@pytest.mark.asyncio
async def test_non_fatal_error_event_settles_ok_and_is_logged(monkeypatch, caplog):
    """AC 1 + AC 2: a turn-terminal but non-fatal error event (claim refused
    / busy, marked `fatal: false` by the loop) must not settle 'error' —
    and the settle path logs what happened (instrumentation)."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        conversation_id=conv,
        next_fire_at=datetime.now().isoformat(timespec="seconds"),
    ))

    async def fake_run(cid, prompt, workspace, **kw):
        yield json.dumps({
            "type": "error",
            "message": "a turn is already running in this conversation",
            "fatal": False,
            "kind": "busy",
        }) + "\n"

    monkeypatch.setattr(loop, "run_agent", fake_run)
    with caplog.at_level("INFO", logger="yaah.scheduler"):
        agent = await get_agent(agent_row["id"])
        assert await sched.fire_agent(agent) == "started"
        await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "ok", (
        "non-fatal error event must not settle 'error'")
    assert any("settled ok" in r.message for r in caplog.records), (
        "settle source must be logged")


@pytest.mark.asyncio
async def test_fatal_error_event_still_settles_error(monkeypatch):
    """AC 3: a fatal in-band error event (provider failure that ends the
    turn, `fatal: true`) still settles 'error'."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        conversation_id=conv,
        next_fire_at=datetime.now().isoformat(timespec="seconds"),
    ))

    async def fake_run(cid, prompt, workspace, **kw):
        yield json.dumps({
            "type": "error",
            "message": "Model API error 500: provider exploded",
            "fatal": True,
        }) + "\n"

    monkeypatch.setattr(loop, "run_agent", fake_run)
    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent) == "started"
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "error"


@pytest.mark.asyncio
async def test_unmarked_error_event_defaults_fatal(monkeypatch):
    """Back-compat: error events without a `fatal` marker (older code paths)
    keep today's behavior — they settle 'error'."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        conversation_id=conv,
        next_fire_at=datetime.now().isoformat(timespec="seconds"),
    ))

    async def fake_run(cid, prompt, workspace, **kw):
        yield json.dumps({"type": "error", "message": "boom"}) + "\n"

    monkeypatch.setattr(loop, "run_agent", fake_run)
    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent) == "started"
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "error"


@pytest.mark.asyncio
async def test_loop_marks_claim_refusals_non_fatal():
    """The loop's two claim-refusal error events carry fatal: False + kind,
    while the hard-failure paths stay fatal."""
    orig = loop._try_begin_run_excluding_remote_lease

    async def fake_begin(cid):
        raise RemoteProtocolError("lease_held")

    loop._try_begin_run_excluding_remote_lease = fake_begin
    try:
        events = [e async for e in loop.run_agent(1, "x", "C:/ws")]
    finally:
        loop._try_begin_run_excluding_remote_lease = orig
    err = json.loads([e for e in events if '"error"' in e][0])
    assert err["fatal"] is False
    assert err["kind"] == "claim_refused"
