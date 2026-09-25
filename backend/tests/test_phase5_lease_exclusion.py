"""Direct database mutation paths must honor active remote transcript leases."""

import pytest

from backend.db import database as db


@pytest.mark.asyncio
async def test_workspace_delete_rejects_leased_conversation_without_partial_relocation():
    workspace = await db.upsert_workspace("C:/leased-workspace")
    conversation_id = await db.create_conversation(
        "Leased chat", workspace=workspace["path"]
    )
    snapshot = await db.get_remote_conversation_snapshot(conversation_id)
    lease = await db.acquire_remote_edit_lease(
        conversation_id, snapshot["revision"], "remote-editor"
    )

    with pytest.raises(db.RemoteProtocolError) as exc_info:
        await db.delete_workspace(workspace["id"])

    assert exc_info.value.code == "lease_held"
    conversation = await db.get_conversation(conversation_id)
    assert conversation["workspace"] == workspace["path"]
    workspaces = await db.list_workspaces()
    assert any(item["id"] == workspace["id"] for item in workspaces)
    assert await db.release_remote_edit_lease(
        conversation_id, lease["lease_token"]
    )


@pytest.mark.asyncio
async def test_agent_retention_rejects_lease_before_deleting_transcript_rows():
    conversation_id = await db.create_conversation("Agent transcript")
    for index in range(4):
        await db.add_message(conversation_id, "user", f"user {index}")
        await db.add_message(conversation_id, "assistant", f"assistant {index}")
    snapshot = await db.get_remote_conversation_snapshot(conversation_id)
    before = await db.get_messages(conversation_id)
    lease = await db.acquire_remote_edit_lease(
        conversation_id, snapshot["revision"], "remote-editor"
    )

    with pytest.raises(db.RemoteProtocolError) as exc_info:
        await db.trim_agent_transcript(conversation_id, keep_runs=1)

    assert exc_info.value.code == "lease_held"
    assert await db.get_messages(conversation_id) == before
    assert await db.release_remote_edit_lease(
        conversation_id, lease["lease_token"]
    )
