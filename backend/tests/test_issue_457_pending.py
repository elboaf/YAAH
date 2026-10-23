"""Issue #457: the backend must expose which remote conversations are parked
on a pending ask_user answer, independent of which window streamed them.

The pending-answer registry in loop.py is keyed "conversation_id:call_id" and
is process-global. The owner-keyed view this test exercises (threading an
optional owner_id through _wait_answer/_ask_user) reports which conversations
of a given remote owner are still waiting for an answer, without leaking local
numeric-keyed conversation state.
"""

import asyncio

from backend.agent import loop as agent_loop


async def _hold_answer(owner: str, conv: str, resolved: list, cancel_ev: asyncio.Event):
    """Simulate one owner's ask_user: block in _wait_answer until resolved."""
    fut = await agent_loop._wait_answer(conv, "call-1", cancel_ev, owner)
    resolved.append(fut.result())


async def test_pending_convs_track_one_owner_parked_conversation():
    owner = "host-a"
    conv = "731"
    assert await agent_loop.remote_pending_running_convs(owner) == []

    cancel_ev = asyncio.Event()
    resolved: list = []
    task = asyncio.create_task(_hold_answer(owner, conv, resolved, cancel_ev))
    # Let _wait_answer register the pending future.
    for _ in range(50):
        if conv in await agent_loop.remote_pending_running_convs(owner):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)

    # The owner's parked conversation is now discoverable.
    assert conv in await agent_loop.remote_pending_running_convs(owner)

    # A different owner has nothing pending.
    assert await agent_loop.remote_pending_running_convs("host-b") == []

    # Resolve the answer: the pending state clears.
    agent_loop.resolve_answer(conv, "call-1", "yes")
    await asyncio.wait_for(task, timeout=1)
    assert resolved == ["yes"]
    for _ in range(50):
        if conv not in await agent_loop.remote_pending_running_convs(owner):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert await agent_loop.remote_pending_running_convs(owner) == []


async def test_pending_convs_scoped_to_owner():
    # Two owners each parked on a conversation that happens to share an ID.
    cancel = asyncio.Event()
    done: list = []
    t1 = asyncio.create_task(_hold_answer("host-a", "42", done, cancel))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(_hold_answer("host-b", "42", done, cancel))
    await asyncio.sleep(0.05)

    # The registry key is "owner:convId:call_id", so both owners' waits are
    # tracked but they never collide on the shared conversation id.
    assert "42" in await agent_loop.remote_pending_running_convs("host-a")
    assert "42" in await agent_loop.remote_pending_running_convs("host-b")

    t1.cancel()
    t2.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)
