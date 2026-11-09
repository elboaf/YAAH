INPUT = 'backend/db/database.py'
with open(INPUT, 'r', encoding='utf-8') as f:
    lines = f.readlines()

output = []
i = 0
added_logging = False
added_fts5_check = False
added_fts5_index = False
added_backfill_fn = False
added_backfill_call = False
replaced_search = False

while i < len(lines):
    line = lines[i]
    
    if not added_logging and line == 'import json\n':
        output.append(line)
        output.append('import logging\n')
        added_logging = True
        i += 1
        continue
    
    if not added_fts5_check and 'await db.executescript(SCHEMA)' in line:
        output.append(line)
        output.append('\n')
        output.append('    # Verify FTS5 extension is available\n')
        output.append('    try:\n')
        output.append("        await db.execute(\"CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_check USING fts5(content, tokenize='trigram')\")\n")
        output.append('        await db.execute("DROP TABLE IF EXISTS _fts5_check")\n')
        output.append('    except aiosqlite.OperationalError as e:\n')
        output.append('        raise RuntimeError(f"SQLite FTS5 not available: {e}") from e\n')
        added_fts5_check = True
        i += 1
        continue
    
    if not added_fts5_index and line.strip() == '"""' and i > 0 and 'messages_remote_revision_delete' in lines[i-1]:
        output.append(line)
        output.append('    """\n')
        output.append('\n')
        output.append('    # FTS5 conversation search index (issue #673)\n')
        output.append('    await db.execute("DROP TABLE IF EXISTS conversation_search_index")\n')
        output.append('    await db.execute("""\n')
        output.append('    CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search_index USING fts5(\n')
        output.append('        doc_id INTEGER, conversation_id INTEGER, role TEXT,\n')
        output.append('        content TEXT, tool_calls TEXT, tool_call_id TEXT,\n')
        output.append("        sub_agent_transcript TEXT, tokenize='trigram'\n")
        output.append('    )\n')
        output.append('    """)\n')
        output.append('\n')
        output.append('    await db.executescript("""\n')
        output.append('    DROP TRIGGER IF EXISTS messages_fts_insert;\n')
        output.append('    CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN\n')
        output.append('        INSERT INTO conversation_search_index(doc_id, conversation_id, role, content, tool_calls, tool_call_id, sub_agent_transcript)\n')
        output.append('        VALUES(NEW.id, NEW.conversation_id, NEW.role, NEW.content, NEW.tool_calls, NEW.tool_call_id, NEW.sub_agent_transcript);\n')
        output.append('    END;\n')
        output.append('    DROP TRIGGER IF EXISTS messages_fts_update;\n')
        output.append('    CREATE TRIGGER messages_fts_update AFTER UPDATE OF role, content, tool_calls, tool_call_id, sub_agent_transcript ON messages BEGIN\n')
        output.append('        DELETE FROM conversation_search_index WHERE doc_id = OLD.id;\n')
        output.append('        INSERT INTO conversation_search_index(doc_id, conversation_id, role, content, tool_calls, tool_call_id, sub_agent_transcript)\n')
        output.append('        VALUES(NEW.id, NEW.conversation_id, NEW.role, NEW.content, NEW.tool_calls, NEW.tool_call_id, NEW.sub_agent_transcript);\n')
        output.append('    END;\n')
        output.append('    DROP TRIGGER IF EXISTS messages_fts_delete;\n')
        output.append('    CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN\n')
        output.append('        DELETE FROM conversation_search_index WHERE doc_id = OLD.id;\n')
        output.append('    END;\n')
        output.append('    """)\n')
        added_fts5_index = True
        i += 1
        continue
    
    if not added_backfill_fn and line.startswith('async def search_conversation_history('):
        output.append('\n')
        output.append('async def _fts5_backfill():\n')
        output.append('    """One-time backfill of FTS5 index from existing messages."""\n')
        output.append('    db = await get_db()\n')
        output.append('    try:\n')
        output.append('        cur = await db.execute("SELECT value FROM app_meta WHERE key = ?", ("fts5_backfill_done",))\n')
        output.append('        if await cur.fetchone() is not None:\n')
        output.append('            return\n')
        output.append('        cur = await db.execute("SELECT COUNT(*) FROM messages")\n')
        output.append('        total = (await cur.fetchone())[0] or 0\n')
        output.append('        if total == 0:\n')
        output.append('            await db.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, ?)", ("fts5_backfill_done", "done"))\n')
        output.append('            await db.commit()\n')
        output.append('            return\n')
        output.append('        logging.info("FTS5 backfill: indexing %d messages", total)\n')
        output.append('        await db.execute("DELETE FROM conversation_search_index")\n')
        output.append('        cur = await db.execute("SELECT id, conversation_id, role, content, tool_calls, tool_call_id, sub_agent_transcript FROM messages ORDER BY id")\n')
        output.append('        rows = await cur.fetchall()\n')
        output.append('        await cur.close()\n')
        output.append('        await db.execute("BEGIN IMMEDIATE")\n')
        output.append('        for row in rows:\n')
        output.append('            await db.execute("INSERT INTO conversation_search_index(doc_id, conversation_id, role, content, tool_calls, tool_call_id, sub_agent_transcript) VALUES(?,?,?,?,?,?,?)", (row["id"], row["conversation_id"], row["role"], row["content"], row["tool_calls"], row["tool_call_id"], row["sub_agent_transcript"]))\n')
        output.append('        await db.commit()\n')
        output.append('        await db.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, ?)", ("fts5_backfill_done", "done"))\n')
        output.append('        await db.commit()\n')
        output.append('        logging.info("FTS5 backfill: completed")\n')
        output.append('    finally:\n')
        output.append('        await db.close()\n')
        output.append('\n')
        added_backfill_fn = True
    
    if not added_backfill_call and line.strip() == '_migrated.add(str(DB_PATH))':
        output.append('    # Run one-time FTS5 backfill (no-op if already done)\n')
        output.append('    await _fts5_backfill()\n')
        output.append('\n')
        output.append(line)
        added_backfill_call = True
        i += 1
        continue
    
    if not replaced_search and line.startswith('async def search_conversation_history('):
        replaced_search = True
        # Write the new search function signature and body
        output.append('async def search_conversation_history(\n')
        output.append('    conversation_id: int, query: str, max_results: int = 10\n')
        output.append(') -> dict:\n')
        output.append('    """Ranked trigram FTS5 search over conversation transcript fields.\n')
        output.append('\n')
        output.append('    Replaces the previous LIKE-based linear scan. The index is maintained\n')
        output.append('    by sync triggers at write time and backfilled on first boot.\n')
        output.append('    Preserves the original response shape for back-compat.\n')
        output.append('    """\n')
        output.append('    query = str(query or "").strip()\n')
        output.append('    if not query:\n')
        output.append('        return {"error": "query must not be empty"}\n')
        output.append('    try:\n')
        output.append('        max_results = max(1, min(int(max_results or 10), 50))\n')
        output.append('    except (TypeError, ValueError):\n')
        output.append('        max_results = 10\n')
        output.append("    safe_query = query.replace(\"'\", \"''\")\n")
        output.append('    db = await get_db()\n')
        output.append('    try:\n')
        output.append('        cur = await db.execute("""SELECT doc_id, rank FROM conversation_search_index WHERE conversation_search_index MATCH ? AND conversation_id = ? ORDER BY rank ASC LIMIT ?""", (safe_query, conversation_id, max_results * 2))\n')
        output.append('        rows = await cur.fetchall()\n')
        output.append('        await cur.close()\n')
        output.append('        if not rows:\n')
        output.append('            return {"matches": [], "count": 0, "truncated": False}\n')
        output.append('        doc_ids = [row["doc_id"] for row in rows]\n')
        output.append('        placeholders = ",".join(["?"] * len(doc_ids))\n')
        output.append('        cur = await db.execute(f"SELECT id, role, content, tool_calls, tool_call_id, sub_agent_transcript FROM messages WHERE id IN ({placeholders})", doc_ids)\n')
        output.append('        messages = {row["id"]: row for row in await cur.fetchall()}\n')
        output.append('        await cur.close()\n')
        output.append('        needle = query.casefold()\n')
        output.append('        candidates = []\n')
        output.append('        for row in rows:\n')
        output.append('            msg_id = row["doc_id"]\n')
        output.append('            if msg_id not in messages:\n')
        output.append('                continue\n')
        output.append('            msg = messages[msg_id]\n')
        output.append('            fields = (("content", msg["content"] or ""), ("tool_calls", json.dumps(msg["tool_calls"], ensure_ascii=False) if msg["tool_calls"] else ""), ("tool_call_id", msg["tool_call_id"] or ""), ("sub_agent_transcript", json.dumps(msg["sub_agent_transcript"], ensure_ascii=False) if msg["sub_agent_transcript"] else ""))\n')
        output.append('            for field, value in fields:\n')
        output.append('                text = str(value)\n')
        output.append('                pos = text.casefold().find(needle)\n')
        output.append('                if pos >= 0:\n')
        output.append('                    start, end = max(0, pos - 160), min(len(text), pos + len(query) + 160)\n')
        output.append('                    excerpt = text[start:end]\n')
        output.append('                    if start:\n')
        output.append('                        excerpt = "\\u2026" + excerpt\n')
        output.append('                    if end < len(text):\n')
        output.append('                        excerpt += "\\u2026"\n')
        output.append('                    candidates.append({"message_id": msg_id, "role": msg["role"], "field": field, "excerpt": excerpt})\n')
        output.append('                    break\n')
        output.append('            if len(candidates) >= max_results:\n')
        output.append('                break\n')
        output.append('        return {"matches": candidates[:max_results], "count": len(candidates[:max_results]), "truncated": len(candidates) > max_results}\n')
        output.append('    finally:\n')
        output.append('        await db.close()\n')
        # Skip the old function body (from signature to next top-level def)
        i += 1
        while i < len(lines) and not lines[i].startswith('async def ') and not lines[i].startswith('def '):
            i += 1
        continue
    
    output.append(line)
    i += 1

with open(INPUT, 'w', encoding='utf-8') as f:
    f.writelines(output)
print('All FTS5 changes applied')