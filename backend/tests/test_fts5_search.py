"""FTS5 conversation search tests (issue #673).

Covers: substring queries, multi-term queries, single-typo queries,
ranked results, write-path sync (both insert paths), delete cleanup,
boot backfill.
"""
import os
import tempfile
import pytest
from backend.db import database

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own database file."""
    db_file = tmp_path / "test_fts5.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(database, "_migrated", set())
    yield db_file


async def test_substring_query():
    """Trigram tokenizer supports substring queries: 'EXECUT' finds 'EXECUTORS'."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    await database.add_message(conv_id, "user", "We discussed the EXECUTORS pattern earlier")
    
    result = await database.search_conversation_history(conv_id, "EXECUT", 10)
    assert result["count"] >= 1
    assert "EXECUTORS" in result["matches"][0]["excerpt"]


async def test_multiterm_query():
    """Multi-term queries work."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    await database.add_message(conv_id, "user", "The database migration failed during deployment")
    await database.add_message(conv_id, "assistant", "Let me check the migration logs")
    
    result = await database.search_conversation_history(conv_id, "migration failed", 10)
    assert result["count"] >= 1
    # First result should contain both terms
    assert "migration" in result["matches"][0]["excerpt"].lower()
    assert "failed" in result["matches"][0]["excerpt"].lower()


async def test_single_typo_query():
    """Single-term typo still surfaces intended matches."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    await database.add_message(conv_id, "user", "The authentication endpoint returned 500")
    
    # Search with a typo
    result = await database.search_conversation_history(conv_id, "authenication", 10)
    assert result["count"] >= 1


async def test_ranked_results():
    """Results are ranked by relevance, not just chronological."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    # Message with more matches should rank higher
    await database.add_message(conv_id, "user", "Error error error on the API endpoint")
    await database.add_message(conv_id, "user", "The API is fine")
    
    result = await database.search_conversation_history(conv_id, "error", 10)
    assert result["count"] >= 2
    # The message with 3 "error"s should rank higher
    assert result["matches"][0]["excerpt"].count("error") > result["matches"][1]["excerpt"].count("error")


async def test_write_path_sync_standard_insert():
    """Messages are searchable immediately after standard add."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    await database.add_message(conv_id, "user", "This is a newly added message")
    
    result = await database.search_conversation_history(conv_id, "newly added", 10)
    assert result["count"] >= 1


async def test_write_path_sync_remote_replay():
    """Messages from remote commit replay are searchable (not silently missing)."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    
    # Remote commit replay: insert directly into messages table (no add_message wrapper)
    db = await database.get_db()
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
        (conv_id, "user", "Message from remote commit replay")
    )
    await db.commit()
    await db.close()
    
    # Verify the FTS trigger indexed it
    result = await database.search_conversation_history(conv_id, "remote commit replay", 10)
    assert result["count"] >= 1


async def test_delete_cleanup_purge():
    """Deleting a conversation leaves no phantom matches."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    await database.add_message(conv_id, "user", "This conversation will be deleted")
    
    # Verify it's searchable
    result = await database.search_conversation_history(conv_id, "deleted", 10)
    assert result["count"] >= 1
    
    # Delete the conversation (triggers FK cascade on messages)
    db = await database.get_db()
    await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    await db.commit()
    await db.close()
    
    # Verify no phantom matches remain (search should find nothing)
    result = await database.search_conversation_history(conv_id, "deleted", 10)
    assert result["count"] == 0


async def test_delete_cleanup_retention_prune():
    """Retention pruning removes old indexed messages."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    
    # Add several messages
    for i in range(5):
        await database.add_message(conv_id, "user", f"Old message {i}")
    for i in range(2):
        await database.add_message(conv_id, "user", f"Recent message {i}")
    
    # Verify all are indexed
    result = await database.search_conversation_history(conv_id, "message", 10)
    assert result["count"] >= 7
    
    # Prune old messages
    db = await database.get_db()
    # Find the cutoff (keep last 2 user rows)
    cur = await db.execute(
        "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id DESC LIMIT 2",
        (conv_id,)
    )
    rows = await cur.fetchall()
    cutoff = rows[-1]["id"] if rows else 0
    await db.execute(
        "DELETE FROM messages WHERE conversation_id = ? AND id < ?",
        (conv_id, cutoff)
    )
    await db.commit()
    await db.close()
    
    # Search for an old message - it should not be found
    result = await database.search_conversation_history(conv_id, "Old message 0", 10)
    assert result["count"] == 0


async def test_boot_backfill():
    """Existing conversations are backfilled on first boot after upgrade."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    # Add messages
    for i in range(3):
        await database.add_message(conv_id, "user", f"Backfill test message {i}")
    
    # Simulate "first boot after upgrade": drop the index and markers,
    # then call get_db() which should run backfill
    db = await database.get_db()
    await db.execute("DELETE FROM conversation_search_index")
    await db.execute("DELETE FROM app_meta WHERE key = 'fts5_backfill_done'")
    await db.commit()
    # Reset migration state so get_db() runs migrations again
    database._migrated.clear()
    await db.close()
    
    # Clear _migrated to simulate fresh process (fresh boot)
    database._migrated.clear()
    # New get_db() call should trigger backfill
    db = await database.get_db()
    await db.close()
    
    # Verify the index was rebuilt by searching
    result = await database.search_conversation_history(conv_id, "Backfill test message", 10)
    assert result["count"] >= 3


async def test_response_shape_unchanged():
    """The response shape is identical to the original implementation."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    await database.add_message(conv_id, "user", "Hello world")
    
    result = await database.search_conversation_history(conv_id, "hello", 10)
    
    assert "matches" in result
    assert "count" in result
    assert "truncated" in result
    if result["count"] > 0:
        match = result["matches"][0]
        assert "message_id" in match
        assert "role" in match
        assert "field" in match
        assert "excerpt" in match


async def test_empty_query():
    """Empty query returns an error."""
    result = await database.search_conversation_history(1, "", 10)
    assert "error" in result


async def test_nonexistent_conversation():
    """Search on non-existent conversation returns empty results."""
    result = await database.search_conversation_history(9999, "anything", 10)
    assert result["count"] == 0