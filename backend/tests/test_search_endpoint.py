"""T3: HTTP search endpoint — human reach across all workspaces, classified results.

Primary test seam for the human-facing conversation search feature.
Seeds real messages through the real HTTP write path, searches via the
endpoint, and verifies:
- ranked matches across all workspaces
- results labeled with kind (chat / tool / sub-agent)
- conversation identity + title and message reference in each result
- result limits respected, truncated flag correct
- malformed/empty queries produce clean errors
- human reach is genuinely global (cross-workspace)
"""

import pytest
from httpx import ASGITransport, AsyncClient

import backend.main as main
import backend.db.database as database
from backend.agent import loop as loop_mod
from backend.agent.config import CONFIG_PATH


@pytest.fixture(autouse=True)
def fresh_db_and_config():
    """Empty registry + empty config + unconsumed seed flag, per test."""
    import os

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(database.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    CONFIG_PATH.unlink(missing_ok=True)
    CONFIG_PATH.write_text('{"access_mode": "full"}', encoding="utf-8")
    database._last_workspace_seeded = False
    database._migrated.discard(str(database.DB_PATH))
    loop_mod._message_queues.clear()
    yield
    loop_mod._message_queues.clear()


@pytest.fixture()
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as c:
        yield c


async def seed_conversation(client, title, workspace, messages):
    """Seed a conversation with messages via the real HTTP write path."""
    resp = await client.post(
        "/api/conversations",
        json={"title": title, "workspace": workspace, "chat_type": "chat"},
    )
    conv_id = resp.json()["id"]
    for msg in messages:
        body = {"role": msg["role"], "content": msg["content"]}
        if msg.get("tool_calls"):
            body["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            body["tool_call_id"] = msg["tool_call_id"]
        if msg.get("sub_agent_transcript"):
            body["sub_agent_transcript"] = msg["sub_agent_transcript"]
        await client.post(f"/api/conversations/{conv_id}/messages", json=body)
    return conv_id


@pytest.mark.anyio
async def test_search_empty_query_returns_400(client):
    """Empty or missing query produces clean 400, not crash."""
    resp = await client.get("/api/search")
    assert resp.status_code == 400
    assert "query" in resp.json()["detail"].lower()

    resp2 = await client.get("/api/search", params={"query": "   "})
    assert resp2.status_code == 400


@pytest.mark.anyio
async def test_search_finds_chat_messages_across_workspaces(client):
    """Ranked matches across all workspaces, each result labeled."""
    conv_a = await seed_conversation(
        client,
        "Project A planning",
        "/home/user/project-a",
        [
            {"role": "user", "content": "Let's design the database schema"},
            {"role": "assistant", "content": "I'll design a PostgreSQL schema with three tables"},
        ],
    )
    conv_b = await seed_conversation(
        client,
        "Project B work",
        "/home/user/project-b",
        [
            {"role": "user", "content": "Can you fix the login page?"},
            {"role": "assistant", "content": "Sure, I'll update the auth flow"},
        ],
    )

    resp = await client.get("/api/search", params={"query": "schema"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    matches = data["matches"]
    assert len(matches) == data["count"]

    # Results are classified with kind
    for m in matches:
        assert "kind" in m
        assert m["kind"] in ("chat", "tool", "sub-agent")

    # Results carry conversation identity
    for m in matches:
        assert "conversation_id" in m
        assert "conversation_title" in m
        assert "message_id" in m


@pytest.mark.anyio
async def test_search_human_reach_is_global(client):
    """Human search spans all workspaces — includes conversations from
    different workspaces, which agents cannot see cross-workspace."""
    conv_a = await seed_conversation(
        client,
        "Chat in workspace A",
        "/workspaces/a",
        [
            {"role": "user", "content": "This is a message unique to workspace A"},
        ],
    )
    conv_b = await seed_conversation(
        client,
        "Chat in workspace B",
        "/workspaces/b",
        [
            {"role": "user", "content": "This is a message unique to workspace B"},
        ],
    )

    # Search for text only in workspace B from the human endpoint
    resp = await client.get(
        "/api/search", params={"query": "unique to workspace B"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    # Found the cross-workspace conversation
    found_conv_ids = {m["conversation_id"] for m in data["matches"]}
    assert conv_b in found_conv_ids


@pytest.mark.anyio
async def test_search_classifies_tool_activity(client):
    """Results matching tool calls or tool messages are classified as 'tool'."""
    await seed_conversation(
        client,
        "Debugging session",
        "/home/user/project",
        [
            {"role": "user", "content": "Why is the build failing?"},
            {
                "role": "assistant",
                "content": "Let me check the build logs",
                "tool_calls": '[{"name":"bash","arguments":{"command":"cat build.log"}}]',
            },
            {
                "role": "tool",
                "content": "error: undefined reference to 'main'",
                "tool_call_id": "call_1",
            },
        ],
    )

    # Search for the tool output content
    resp = await client.get(
        "/api/search", params={"query": "undefined reference"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1, f"Expected matches but got: {data}"
    kinds = {m["kind"] for m in data["matches"]}
    assert "tool" in kinds


@pytest.mark.anyio
async def test_search_classifies_subagent_transcripts(client):
    """Results matching sub-agent transcripts are classified as 'sub-agent'."""
    import json

    transcript = json.dumps([
        {"role": "assistant", "content": "Exploring the codebase..."},
        {"role": "user", "content": "Found the relevant files"},
    ])

    conv_id = await seed_conversation(
        client,
        "Agent delegation session",
        "/home/user/project",
        [
            {"role": "user", "content": "Find all the bug reports"},
            {"role": "assistant", "content": "I'll delegate this exploration"},
        ],
    )

    # Insert a sub-agent transcript message via the database layer
    # (the HTTP endpoint doesn't accept sub_agent_transcript directly)
    async with database.open_db() as db:
        await db.execute(
            """INSERT INTO messages (conversation_id, role, content, sub_agent_transcript)
               VALUES (?, ?, ?, ?)""",
            (conv_id, "assistant", "Delegation complete", transcript)
        )
        await db.commit()

    # Search for text that appears in the serialized JSON transcript
    resp = await client.get(
        "/api/search", params={"query": "Exploring the codebase"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1, f"Expected matches but got: {data}"
    kinds = {m["kind"] for m in data["matches"]}
    assert "sub-agent" in kinds


@pytest.mark.anyio
async def test_search_respects_max_results_and_truncated(client):
    """Result limits are respected; truncated flag is correct."""
    # Seed many conversations with searchable content
    conv_ids = []
    for i in range(15):
        conv = await seed_conversation(
            client,
            f"Conversation {i}",
            f"/home/user/project-{i % 3}",
            [
                {"role": "user", "content": f"Discussing topic alpha in chat {i}"},
            ],
        )
        conv_ids.append(conv)

    # Request only 3 results
    resp = await client.get(
        "/api/search",
        params={"query": "topic alpha", "max_results": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    assert len(data["matches"]) == 3
    assert data["truncated"] is True

    # Request all
    resp2 = await client.get(
        "/api/search",
        params={"query": "topic alpha", "max_results": 50},
    )
    data2 = resp2.json()
    assert data2["count"] == 15
    assert data2["truncated"] is False


@pytest.mark.anyio
async def test_search_results_sorted_by_relevance(client):
    """FTS5 rank puts most relevant matches first."""
    # One conversation has the exact phrase; another has a loose match
    await seed_conversation(
        client,
        "Exact match conversation",
        "/home/user/project",
        [
            {"role": "user", "content": "The database migration failed"},
        ],
    )
    await seed_conversation(
        client,
        "Loose match conversation",
        "/home/user/project",
        [
            {"role": "user", "content": "We should probably think about migrating the database someday"},
        ],
    )

    resp = await client.get(
        "/api/search", params={"query": "database migration failed"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    # The exact match conversation should be first
    top_match = data["matches"][0]
    assert top_match["conversation_title"] == "Exact match conversation"


@pytest.mark.anyio
async def test_search_conversation_identity_is_correct(client):
    """Each result's conversation_id matches its title."""
    conv1 = await seed_conversation(
        client, "First chat", "/ws1",
        [{"role": "user", "content": "Searching for first chat content"}],
    )
    conv2 = await seed_conversation(
        client, "Second chat", "/ws2",
        [{"role": "user", "content": "Searching for second chat content"}],
    )

    resp = await client.get(
        "/api/search", params={"query": "Searching for"}
    )
    assert resp.status_code == 200
    data = resp.json()

    for m in data["matches"]:
        # Verify conversation_id and title are consistent
        if m["conversation_id"] == conv1:
            assert m["conversation_title"] == "First chat"
        elif m["conversation_id"] == conv2:
            assert m["conversation_title"] == "Second chat"