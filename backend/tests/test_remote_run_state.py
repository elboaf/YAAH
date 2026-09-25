"""Remote run claims remain isolated by owner and conversation identity."""

import pytest

from backend.agent.remote_run_state import RemoteRunRegistry


@pytest.mark.asyncio
async def test_same_conversation_id_can_run_for_distinct_owners():
    runs = RemoteRunRegistry()

    assert await runs.claim("host-a", "731")
    assert await runs.claim("host-b", "731")
    assert not await runs.claim("host-a", "731")
    assert await runs.is_running("host-a", "731")
    assert await runs.is_running("host-b", "731")

    assert await runs.release("host-a", "731")
    assert not await runs.is_running("host-a", "731")
    assert await runs.is_running("host-b", "731")
    assert not await runs.release("host-a", "731")


@pytest.mark.asyncio
async def test_cancel_signal_is_scoped_to_exact_owner_and_chat():
    runs = RemoteRunRegistry()

    assert await runs.claim("host-a", "731")
    assert await runs.claim("host-b", "731")
    event_a = await runs.cancel_event("host-a", "731")
    event_b = await runs.cancel_event("host-b", "731")

    assert event_a is not None and event_b is not None and event_a is not event_b
    assert await runs.cancel("host-a", "731")
    assert event_a.is_set()
    assert not event_b.is_set()
    assert not await runs.cancel("host-c", "731")


@pytest.mark.asyncio
async def test_run_registry_rejects_empty_owner_or_conversation_id():
    runs = RemoteRunRegistry()
    with pytest.raises(ValueError):
        await runs.claim("", "731")
    with pytest.raises(ValueError):
        await runs.claim("host-a", "")
