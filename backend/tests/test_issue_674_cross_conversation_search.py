"""Issue #674: Retrieval T2 - agent cross-conversation reach.

Tests the expanded search scope through the tool executor seam:
- Single-conversation search (default, back-compat)
- Workspace-wide search (main chats only)
- Related-workspace search
- Sub-agent pinning to own conversation
"""
import pytest
import os
from backend.db import database
from backend.agent.tools import execute_tool
from backend.db.database import (
    create_conversation, add_message,
    set_related_workspaces, get_related_workspaces,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own database file.

    DB_PATH is read at call time from the module global, so the fixture
    must rebind it (the env var alone is only read at import time).
    """
    db_file = tmp_path / "test_674.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(database, "_migrated", set())
    yield db_file


@pytest.mark.asyncio
async def test_single_conversation_search_back_compat(tmp_path):
    """Default search is single-conversation (back-compat)."""
    # Two conversations in the same workspace
    ws = str(tmp_path)
    cid1 = await create_conversation("chat one", workspace=ws)
    cid2 = await create_conversation("chat two", workspace=ws)
    await add_message(cid1, "user", "this is chat one")
    await add_message(cid2, "user", "this is chat two")

    # Search in conversation 1 - should NOT find chat two's message
    result = await execute_tool(
        "search_conversation_history",
        {"query": "chat two"},
        ws,
        conversation_id=cid1,
    )
    assert result["count"] == 0

    # Search in conversation 2 - should find its own message
    result = await execute_tool(
        "search_conversation_history",
        {"query": "chat two"},
        ws,
        conversation_id=cid2,
    )
    assert result["count"] == 1
    # Back-compat: single-chat results don't have conversation_id field
    assert "conversation_id" not in result["matches"][0]


@pytest.mark.asyncio
async def test_workspace_reach_searches_all_workspace_conversations(tmp_path):
    """workspace_reach='workspace' searches all conversations in the workspace."""
    ws = str(tmp_path)
    cid1 = await create_conversation("chat one", workspace=ws)
    cid2 = await create_conversation("chat two", workspace=ws)
    cid3 = await create_conversation("chat three", workspace=ws)
    await add_message(cid1, "user", "unique content alpha")
    await add_message(cid2, "user", "unique content beta")
    await add_message(cid3, "user", "unique content gamma")

    # Search for "beta" from conversation 1 with workspace reach
    result = await execute_tool(
        "search_conversation_history",
        {"query": "unique content beta", "workspace_reach": "workspace"},
        ws,
        conversation_id=cid1,
    )
    assert result["count"] == 1
    match = result["matches"][0]
    # Cross-chat results identify their source conversation
    assert match["conversation_id"] == cid2
    assert match["conversation_title"] == "chat two"


@pytest.mark.asyncio
async def test_workspace_reach_returns_multiple_conversations(tmp_path):
    """workspace_reach returns matches from multiple conversations."""
    ws = str(tmp_path)
    cid1 = await create_conversation("chat one", workspace=ws)
    cid2 = await create_conversation("chat two", workspace=ws)
    cid3 = await create_conversation("chat three", workspace=ws)
    await add_message(cid1, "user", "shared phrase here")
    await add_message(cid2, "user", "shared phrase also here")
    await add_message(cid3, "user", "different phrase")

    result = await execute_tool(
        "search_conversation_history",
        {"query": "shared phrase", "workspace_reach": "workspace"},
        ws,
        conversation_id=cid1,
    )
    # Should find matches in cid1 and cid2
    assert result["count"] == 2
    conv_ids = {m["conversation_id"] for m in result["matches"]}
    assert conv_ids == {cid1, cid2}


@pytest.mark.asyncio
async def test_related_workspace_search(tmp_path):
    """workspace_reach='related' searches only related workspaces."""
    ws1 = str(tmp_path / "ws1")
    ws2 = str(tmp_path / "ws2")
    os.makedirs(ws1, exist_ok=True)
    os.makedirs(ws2, exist_ok=True)

    cid1 = await create_conversation("chat in ws1", workspace=ws1)
    cid2 = await create_conversation("chat in ws2", workspace=ws2)
    await add_message(cid1, "user", "message in ws1")
    await add_message(cid2, "user", "message in ws2")

    # Without related workspaces configured, 'related' finds nothing
    result = await execute_tool(
        "search_conversation_history",
        {"query": "message in ws2", "workspace_reach": "related"},
        ws1,
        conversation_id=cid1,
    )
    assert result["count"] == 0

    # Configure ws2 as related to ws1
    await set_related_workspaces(ws1, [ws2])

    # Now 'related' should find ws2's conversation
    result = await execute_tool(
        "search_conversation_history",
        {"query": "message in ws2", "workspace_reach": "related"},
        ws1,
        conversation_id=cid1,
    )
    assert result["count"] == 1
    assert result["matches"][0]["conversation_id"] == cid2

    # 'workspace' should find both ws1's own and related ws2's conversations
    result = await execute_tool(
        "search_conversation_history",
        {"query": "message", "workspace_reach": "workspace"},
        ws1,
        conversation_id=cid1,
    )
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_subagent_pinned_to_own_conversation(tmp_path):
    """Sub-agents only see their own conversation, never the wider history.

    This is enforced by the tool registry: sub-agents don't get the
    search_conversation_history tool at all (see subagents.py _ALWAYS_EXCLUDED).
    This test verifies the registry excludes the tool for sub-agents.
    """
    from backend.agent.subagents import _ALWAYS_EXCLUDED

    assert "search_conversation_history" in _ALWAYS_EXCLUDED, \
        "search_conversation_history must be excluded from sub-agent tool sets"


@pytest.mark.asyncio
async def test_empty_query_error(tmp_path):
    """Empty query returns an error."""
    ws = str(tmp_path)
    cid = await create_conversation("chat", workspace=ws)
    result = await execute_tool(
        "search_conversation_history",
        {"query": ""},
        ws,
        conversation_id=cid,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_no_matches(tmp_path):
    """Query with no matches returns empty results."""
    ws = str(tmp_path)
    cid = await create_conversation("chat", workspace=ws)
    await add_message(cid, "user", "hello world")
    result = await execute_tool(
        "search_conversation_history",
        {"query": "xyzzy not found"},
        ws,
        conversation_id=cid,
    )
    assert result["count"] == 0
    assert result["matches"] == []
