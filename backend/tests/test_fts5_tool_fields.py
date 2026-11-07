"""FTS5 search tests for tool_calls and sub_agent_transcript fields."""
import json
import pytest
from backend.db import database

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_fts5_tools.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(database, "_migrated", set())
    yield db_file


async def test_tool_calls_field_is_indexed():
    """A query matching only tool_calls returns the correct field."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    await database.add_message(
        conv_id, "assistant", "Let me search those files",
        tool_calls=[{"name": "search_files", "arguments": {"pattern": "foo"}}]
    )

    result = await database.search_conversation_history(conv_id, "search_files", 10)
    assert result["count"] >= 1
    # FTS5 matches via shared trigrams; Python picks first field with literal query
    # tool_calls contains the literal query string
    found_tool = False
    for m in result["matches"]:
        if m["field"] == "tool_calls":
            found_tool = True
            break
    assert found_tool, "tool_calls field should be in results"


async def test_sub_agent_transcript_field_is_indexed():
    """A query matching only sub_agent_transcript returns the correct field."""
    conv_id = 1
    await database.create_conversation("Test", "test-ws")
    transcript = json.dumps({
        "task": "run the tests",
        "result": "all tests passed"
    })
    await database.add_message(
        conv_id, "assistant", "I'll spawn a sub-agent for that",
        sub_agent_transcript=transcript
    )

    result = await database.search_conversation_history(conv_id, "spawn a sub-agent", 10)
    assert result["count"] >= 1
    # Content contains the literal query
    assert result["matches"][0]["field"] == "content"

    # Search for a string that only literally appears in the transcript
    result = await database.search_conversation_history(conv_id, "all tests passed", 10)
    assert result["count"] >= 1
    # Only transcript contains the literal query string
    found_transcript = False
    for m in result["matches"]:
        if m["field"] == "sub_agent_transcript":
            found_transcript = True
            break
    assert found_transcript, "sub_agent_transcript field should be in results"
