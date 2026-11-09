with open('backend/tests/test_fts5_search.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = """    # Simulate "first boot after upgrade": drop the index and markers,
    # then call get_db() which should run backfill
    db = await database.get_db()
    await db.execute("DELETE FROM conversation_search_index")
    await db.execute("DELETE FROM app_meta WHERE key = 'fts5_backfill_done'")
    await db.commit()
    await db.close()"""

new = """    # Simulate "first boot after upgrade": drop the index and markers,
    # then call get_db() which should run backfill
    db = await database.get_db()
    await db.execute("DELETE FROM conversation_search_index")
    await db.execute("DELETE FROM app_meta WHERE key = 'fts5_backfill_done'")
    await db.commit()
    # Reset migration state so get_db() runs migrations again
    database._migrated.clear()
    await db.close()"""

content = content.replace(old, new)

with open('backend/tests/test_fts5_search.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed backfill test (moved _migrated.clear())")
