"""#1245: cost-tracking accuracy — per-call persist + session-cumulative chip.

Two bugs against the #627 feature:

1. The usage wire event carried turn_cost (this turn's spend only), and the
   frontend wrote it over the session cumulative — the chip reset every turn
   and showed a fraction of the session's real cost.
2. Cost persisted only when a turn completed naturally. A cancelled, errored,
   or budget-exhausted turn lost the spend of the model calls it already made
   (OpenRouter bills per call; the calls already happened).
"""
import json

import pytest

from backend.agent import loop
from backend.db.database import (
    add_conversation_cost,
    create_conversation,
    get_conversation,
)


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


def _usage(prompt_tokens: int, cost: float, completion: int) -> dict:
    """The usage chunk OpenRouter sends (cost in dollars, per-call)."""
    return {
        "type": "usage",
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion,
            "cost": cost,
        },
    }


@pytest.mark.asyncio
async def test_cost_persists_per_call_not_per_turn(fake_model, tmp_path):
    """Each model call's cost lands in the DB immediately — a turn that later
    fails or is cancelled keeps the spend of the calls it already made."""
    cid = await create_conversation("cost-per-call")
    fake_model.append([
        {"type": "content", "text": "calling a tool"},
        _usage(1000, 0.01, 10),
        {"type": "tool_calls", "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})},
        }]},
    ])
    # Second call fails mid-turn (no finish): the old code would have
    # discarded the first call's $0.01.
    from backend.agent import model_client

    # The loop retries a ModelError once, so the failure must be scripted
    # twice (attempt 1 and the retry). The failing stream yields its usage
    # first (the provider billed the call) and then raises.
    from backend.agent import model_client

    class BilledThenFailStream:
        """Async iterator: usage event, then ModelError."""

        def __init__(self, usage):
            self._usage = usage
            self._used = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._used:
                self._used = True
                return self._usage
            raise model_client.ModelError("provider blew up")

    scripts = fake_model
    scripts.append([{"type": "fail"}])
    scripts.append([{"type": "fail"}])

    orig_chat = loop.model_client.chat

    async def failing_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        if events and events[0].get("type") == "fail":
            return BilledThenFailStream(_usage(2000, 0.02, 20))
        return FakeStream(events)

    loop.model_client.chat = failing_chat
    try:
        events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    finally:
        loop.model_client.chat = orig_chat

    # A tool cycle ran first (the tool result needs a following model call),
    # so the turn ends on the ModelError path.
    assert any(e["type"] == "error" for e in events)

    conv = await get_conversation(cid)
    # All billed calls persisted even though the turn failed: the tool
    # call ($0.01) plus the two failing attempts (the retry re-bills at
    # $0.02 each). The old turn-end persist would have written none of
    # them.
    assert conv["cost_spent"] == pytest.approx(0.05)
    assert conv["completion_tokens_total"] == 50


@pytest.mark.asyncio
async def test_usage_event_carries_session_cumulative(fake_model, tmp_path):
    """cost_spent on the wire event is the SESSION total (pre-turn stored
    total + this turn's spend so far), not the per-turn figure."""
    cid = await create_conversation("cost-session-cumulative")
    # Pre-turn stored total from earlier turns.
    await add_conversation_cost(cid, 0.50, 100)

    fake_model.append([
        {"type": "content", "text": "A real answer."},
        _usage(1234, 0.0058, 42),
        {"type": "finish"},
    ])

    events = await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    usage = next(e for e in events if e["type"] == "usage")

    assert usage["usage_tokens"] == 1234
    # 0.50 pre-turn + 0.0058 this call — NOT the bare turn figure 0.0058.
    assert usage["cost_spent"] == pytest.approx(0.5058)
    # turn_cost stays for old frontends (this turn only).
    assert usage["turn_cost"] == pytest.approx(0.0058)

    # The DB total matches what the chip will show.
    conv = await get_conversation(cid)
    assert conv["cost_spent"] == pytest.approx(0.5058)


@pytest.mark.asyncio
async def test_cost_none_provider_persists_completion_tokens_only(fake_model, tmp_path):
    """Providers without cost reporting keep working: completion tokens
    accumulate, cost stays NULL (no fake zeros)."""
    cid = await create_conversation("cost-none-provider")
    fake_model.append([
        {"type": "content", "text": "A real answer."},
        {"type": "usage", "usage": {"prompt_tokens": 500, "completion_tokens": 77}},
        {"type": "finish"},
    ])

    events = await collect(loop.run_agent(cid, "hi", str(tmp_path)))
    usage = next(e for e in events if e["type"] == "usage")

    assert usage["cost_spent"] is None

    conv = await get_conversation(cid)
    assert conv["cost_spent"] is None
    assert conv["completion_tokens_total"] == 77


@pytest.mark.asyncio
async def test_no_usage_event_no_persist(fake_model, tmp_path):
    """A provider that sends no usage at all must not write zero rows."""
    cid = await create_conversation("no-usage-no-persist")
    fake_model.append([{"type": "content", "text": "A real answer."}, {"type": "finish"}])

    await collect(loop.run_agent(cid, "hi", str(tmp_path)))

    conv = await get_conversation(cid)
    assert conv["cost_spent"] is None
    assert conv["completion_tokens_total"] is None


@pytest.mark.asyncio
async def test_cancelled_sub_agent_still_persists_its_billed_cost(monkeypatch, tmp_path):
    """A parent Stop landing INSIDE the cost write must not discard it.

    The sub-agent's stream is consumed by a task the parent cancels on Stop
    (subagents.py consume/cancel race), and CancelledError is a BaseException
    — so an unguarded write would be interrupted before its commit() and
    the increment silently lost, even though the provider had already billed
    the call. This cancels during the write window and asserts the increment
    survived.
    """
    import asyncio

    from backend.agent import subagents
    from backend.db import database as dbmod
    from backend.db.database import create_conversation, get_conversation

    cid = await create_conversation("sub-cost-cancel")
    real_add_cost = dbmod.add_conversation_cost
    entered = asyncio.Event()

    async def delayed_cost(conversation_id, cost, completion_tokens):
        # Signal, then stall BEFORE the real (committing) write so the
        # cancel lands squarely inside the write window.
        entered.set()
        await asyncio.sleep(0.3)
        await real_add_cost(conversation_id, cost, completion_tokens)

    monkeypatch.setattr(subagents, "add_conversation_cost", delayed_cost)

    async def sub_events():
        yield {"type": "content", "text": "sub findings"}
        yield {"type": "usage", "usage": {"cost": 0.07, "completion_tokens": 5}}
        yield {"type": "finish"}

    async def fake_chat(messages, tools=None, stream=True):
        # NB: the `stream` kwarg must not shadow a generator named `stream`
        # (a shadowed call reads as "'bool' object is not callable").
        return sub_events()

    monkeypatch.setattr(subagents.model_client, "chat", fake_chat)

    cancel_ev = asyncio.Event()
    task = asyncio.create_task(
        subagents.run_sub_agent(
            subagents.get_agent_def("explore"),
            "look",
            str(tmp_path),
            cancel_ev=cancel_ev,
            conversation_id=cid,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    cancel_ev.set()  # parent Stop, mid-write
    await asyncio.wait_for(task, timeout=5)

    conv = await get_conversation(cid)
    assert conv["cost_spent"] == pytest.approx(0.07)
    assert conv["completion_tokens_total"] == 5


@pytest.mark.asyncio
async def test_sub_agent_cost_rides_sub_agent_done(monkeypatch, tmp_path):
    """A sub-agent's billed calls persist to the parent conversation's
    total (#1245): OpenRouter bills the sub's calls too, and the old code
    never wrote them. The sub's spend rides sub_agent_done.cost_spent."""
    from backend.agent import subagents
    from backend.db.database import create_conversation

    cid = await create_conversation("sub-cost")
    # The sub-agent itself runs a scripted stream directly.
    async def sub_stream():
        yield {"type": "content", "text": "sub findings"}
        yield {"type": "usage", "usage": {"cost": 0.07, "completion_tokens": 5}}
        yield {"type": "finish"}

    async def fake_sub_chat(messages, tools=None, stream=True):
        return sub_stream()

    monkeypatch.setattr(subagents.model_client, "chat", fake_sub_chat)

    async def run_sub():
        return await subagents.run_sub_agent(
            subagents.get_agent_def("explore"), "look", str(tmp_path)
        )

    result = await run_sub()
    assert result["cost_spent"] == pytest.approx(0.07)


@pytest.mark.asyncio
async def test_sub_agent_cost_persists_through_the_parent_loop(fake_model, tmp_path):
    """The sub's billed spend reaches the DB when spawned by the PARENT
    LOOP (#1245), not just the wire event.

    The parent-loop path is the one that matters: the chip raises the
    session total on sub_agent_done and then the parent's next usage tick
    overwrites it with the DB total. If the sub's spend never lands in the
    DB, the chip bumps and then drops back -- so this asserts the DB, and
    drives the real producer (run_agent -> spawn_batch -> sub-agent) rather
    than calling run_sub_agent directly.
    """
    from backend.db.database import create_conversation, get_conversation

    cid = await create_conversation("sub-cost-parent-loop")
    # Parent: one spawn_agent call.
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
                        "prompt": "say hi",
                    }),
                },
            }],
        },
    ])
    # The sub-agent's single billed model call (same client, same queue).
    fake_model.append([
        {"type": "content", "text": "sub hi"},
        _usage(10, 0.07, 5),
        {"type": "finish"},
    ])
    # Parent: wrap up.
    fake_model.append([{"type": "content", "text": "done"}, {"type": "finish"}])

    events = await collect(loop.run_agent(cid, "delegate", str(tmp_path)))

    sub_done = [e for e in events if e.get("type") == "sub_agent_done"]
    assert sub_done, "no sub_agent_done event"
    assert sub_done[0]["cost_spent"] == pytest.approx(0.07)

    # The wire event alone is not enough: the parent's next usage tick
    # replaces the chip's value with the DB total, so the spend must be
    # IN the DB or the chip visibly drops back.
    conv = await get_conversation(cid)
    assert conv["cost_spent"] == pytest.approx(0.07)
    assert conv["completion_tokens_total"] == 5
