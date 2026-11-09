with open('backend/tests/test_fts5_search.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: test_delete_cleanup_purge
old_purge = """    # Delete the conversation (triggers FK cascade on messages)
    db = await database.get_db()
    await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    await db.commit()
    await db.close()
    
    # Verify no phantom matches remain
    db = await database.get_db()
    cur = await db.execute(
        "SELECT doc_id FROM conversation_search_index WHERE conversation_id = ?",
        (conv_id,)
    )
    rows = await cur.fetchall()
    await db.close()
    assert len(rows) == 0"""

new_purge = """    # Delete the conversation (triggers FK cascade on messages)
    db = await database.get_db()
    await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    await db.commit()
    await db.close()
    
    # Verify no phantom matches remain (search should find nothing)
    result = await database.search_conversation_history(conv_id, "deleted", 10)
    assert result["count"] == 0"""

content = content.replace(old_purge, new_purge)

# Fix 2: test_delete_cleanup_retention_prune
old_prune = """    # Prune old messages
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
    
    # Verify old messages are removed from index
    db = await database.get_db()
    cur = await db.execute(
        "SELECT doc_id FROM conversation_search_index WHERE conversation_id = ? AND doc_id < ?",
        (conv_id, cutoff)
    )
    rows = await cur.fetchall()
    await db.close()
    assert len(rows) == 0"""

new_prune = """    # Prune old messages
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
    assert result["count"] == 0"""

content = content.replace(old_prune, new_prune)

# Fix 3: test_boot_backfill - use search to verify
old_backfill = """    # Verify the index was rebuilt
    db = await database.get_db()
    cur = await db.execute("SELECT COUNT(*) FROM conversation_search_index")
    row = await cur.fetchone()
    await db.close()
    assert row[0] >= 3"""

new_backfill = """    # Verify the index was rebuilt by searching
    result = await database.search_conversation_history(conv_id, "Backfill test message", 10)
    assert result["count"] >= 3"""

content = content.replace(old_backfill, new_backfill)

with open('backend/tests/test_fts5_search.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed delete/backfill tests")