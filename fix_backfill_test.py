with open('backend/tests/test_fts5_search.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = """    # New get_db() call should trigger backfill
    db = await database.get_db()
    await db.close()"""

new = """    # Clear _migrated to simulate fresh process (fresh boot)
    database._migrated.clear()
    # New get_db() call should trigger backfill
    db = await database.get_db()
    await db.close()"""

content = content.replace(old, new)

with open('backend/tests/test_fts5_search.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed backfill test")
