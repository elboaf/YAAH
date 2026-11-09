INPUT = 'backend/db/database.py'
with open(INPUT, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: Remove the unconditional DROP TABLE IF EXISTS
content = content.replace('    await db.execute("DROP TABLE IF EXISTS conversation_search_index")\n', '')

# Fix 2: Correct FTS5 column types (all TEXT)
old_fts5_create = """    CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search_index USING fts5(
        doc_id INTEGER, conversation_id INTEGER, role TEXT,
        content TEXT, tool_calls TEXT, tool_call_id TEXT,
        sub_agent_transcript TEXT, tokenize='trigram'
    )"""

new_fts5_create = """    CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search_index USING fts5(
        doc_id TEXT, conversation_id TEXT, role TEXT,
        content TEXT, tool_calls TEXT, tool_call_id TEXT,
        sub_agent_transcript TEXT, tokenize='trigram'
    )"""

content = content.replace(old_fts5_create, new_fts5_create)

with open(INPUT, 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed FTS5 column types and removed unconditional drop")
