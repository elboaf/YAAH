with open('backend/tests/test_fts5_search.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Simplify ranking test to just check both are returned
old_rank = """    result = await database.search_conversation_history(conv_id, "error", 10)
    # Both should be returned (ranking ensures both matching docs appear)
    assert result["count"] >= 2"""

new_rank = """    result = await database.search_conversation_history(conv_id, "error", 10)
    # Both matching messages should be returned
    assert result["count"] == 2
    # Check that both contain "error"
    for m in result["matches"]:
        assert "error" in m["excerpt"].lower()"""

content = content.replace(old_rank, new_rank)

with open('backend/tests/test_fts5_search.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed ranking test")
