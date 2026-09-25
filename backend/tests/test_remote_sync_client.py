import pytest
from backend.db.database import (
    get_remote_messages, list_remote_conversations, queue_remote_commit,
    upsert_remote_conversation, pending_remote_commits, acknowledge_remote_commit,
)

@pytest.mark.asyncio
async def test_pending_commit_survives_refresh_and_acknowledges():
    host_id = "pending-owner"
    conversation = {"id": "17", "title": "Chat", "workspace": None, "revision": "r:4"}
    await upsert_remote_conversation(host_id, conversation, [{"id": 1, "role": "user", "content": "old"}])
    commit_id = await queue_remote_commit(host_id, "17", "r:4", conversation, [{"id": 1, "role": "user", "content": "pending"}])
    await upsert_remote_conversation(host_id, {**conversation, "revision": "r:5"}, [{"id": 2, "role": "user", "content": "fresh"}])
    assert (await list_remote_conversations(host_id))[0]["sync_status"] == "pending"
    assert (await get_remote_messages(host_id, "17"))[0]["content"] == "pending"
    queued = await pending_remote_commits(host_id, "17")
    assert queued[0]["commit_id"] == commit_id
    assert queued[0]["base_revision"] == "r:4"
    assert await acknowledge_remote_commit(host_id, "17", commit_id, "r:5")
    assert (await list_remote_conversations(host_id))[0]["sync_status"] == "synced"
    assert await pending_remote_commits(host_id, "17") == []
