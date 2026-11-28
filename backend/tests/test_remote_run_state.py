"""Remote run claims remain isolated by owner and conversation identity."""

import asyncio

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


@pytest.mark.asyncio
async def test_running_convs_lists_all_owned_conversations():
    runs = RemoteRunRegistry()

    # No owner -> no running conversations.
    assert await runs.running_convs("host-a") == []

    # Claim two distinct conversations for the same owner.
    assert await runs.claim("host-a", "1")
    assert await runs.claim("host-a", "2")
    assert set(await runs.running_convs("host-a")) == {"1", "2"}

    # A different owner is unaffected.
    assert await runs.running_convs("host-b") == []

    # Release one; still running because the other is active.
    assert await runs.release("host-a", "1")
    assert set(await runs.running_convs("host-a")) == {"2"}

    # Release the last active run for the owner -> nothing running.
    assert await runs.release("host-a", "2")
    assert await runs.running_convs("host-a") == []


@pytest.mark.asyncio
async def test_claim_with_grace_acquires_when_slot_is_free():
    runs = RemoteRunRegistry()
    assert await runs.claim_with_grace("host-a", "731")
    assert await runs.is_running("host-a", "731")


@pytest.mark.asyncio
async def test_claim_with_grace_rides_out_a_stale_claim_released_mid_grace():
    """A finished/abandoned turn's stream teardown releases the slot within
    milliseconds; a fresh send must succeed instead of 409ing (#903)."""
    runs = RemoteRunRegistry()
    assert await runs.claim("host-a", "731")

    async def release_soon():
        await asyncio.sleep(0.2)
        await runs.release("host-a", "731")

    releaser = asyncio.create_task(release_soon())
    assert await runs.claim_with_grace("host-a", "731", grace_seconds=2.0,
                                       poll_seconds=0.05)
    await releaser
    # The grace acquire held the slot afterwards.
    assert await runs.is_running("host-a", "731")


@pytest.mark.asyncio
async def test_claim_with_grace_refuses_when_the_claim_is_still_live():
    """A genuinely live run (claim held past the grace) still gets a clean
    refusal, so the handler can 409 with a message the client can act on."""
    runs = RemoteRunRegistry()
    assert await runs.claim("host-a", "731")
    started = asyncio.get_running_loop().time()
    assert not await runs.claim_with_grace("host-a", "731", grace_seconds=0.3,
                                           poll_seconds=0.05)
    elapsed = asyncio.get_running_loop().time() - started
    assert 0.3 <= elapsed < 2.0  # waited the grace, then refused
    assert await runs.is_running("host-a", "731")  # the live run is untouched


@pytest.mark.asyncio
async def test_claim_with_grace_rejects_empty_owner_or_conversation_id():
    runs = RemoteRunRegistry()
    with pytest.raises(ValueError):
        await runs.claim_with_grace("", "731")
    with pytest.raises(ValueError):
        await runs.claim_with_grace("host-a", "")
