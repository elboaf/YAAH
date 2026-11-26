"""#855: the multi-goal checklist (the ycode "TodoWrite" mechanism).

Covers (1) the todo module (schemas, parse, format, tail message), (2) the
`session_todos` DB column migration + round-trip, and (3) the TodoWrite /
TodoRead executors persisting to and reading from the conversation row.
"""
import json

import pytest

from backend.agent import todo
from backend.agent import tools as tools_mod
from backend.db import database


# --------------------------------------------------------------- schemas

def test_todo_schemas_are_registered():
    # The two tools are in the schema set the model sees.
    names = {s["function"]["name"] for s in tools_mod.TOOLS_SCHEMA}
    assert todo.TODO_WRITE_TOOL in names
    assert todo.TODO_READ_TOOL in names


def test_todo_write_schema_shape():
    schema = tools_mod.SCHEMAS[todo.TODO_WRITE_TOOL]
    params = schema["function"]["parameters"]
    assert "todos" in params["properties"]
    item = params["properties"]["todos"]["items"]
    assert set(item["properties"]) == {"content", "status", "priority"}
    assert item["properties"]["status"]["enum"] == [
        todo.PENDING, todo.IN_PROGRESS, todo.COMPLETED
    ]
    assert item["properties"]["priority"]["enum"] == [
        todo.HIGH, todo.MEDIUM, todo.LOW
    ]


# ------------------------------------------------------- parse / normalize

def test_parse_todo_result_returns_normalized_items():
    result = {
        "todos": [
            {"content": "Fix bug A", "status": "completed", "priority": "high"},
            {"content": "Add feature B", "status": "in_progress", "priority": "medium"},
            {"content": "Write tests", "status": "pending", "priority": "low"},
        ]
    }
    items = todo.parse_todo_result(result)
    assert items == [
        {"content": "Fix bug A", "status": "completed", "priority": "high"},
        {"content": "Add feature B", "status": "in_progress", "priority": "medium"},
        {"content": "Write tests", "status": "pending", "priority": "low"},
    ]


def test_parse_todo_result_normalizes_bad_values():
    # Bad status/priority fall back to the defaults the model would use.
    result = {
        "todos": [
            {"content": "  Trimmed  ", "status": "weird", "priority": "nope"},
        ]
    }
    items = todo.parse_todo_result(result)
    assert items == [
        {"content": "Trimmed", "status": "pending", "priority": "medium"}
    ]


def test_parse_todo_result_drops_unusable_items():
    result = {
        "todos": [
            {"content": "good", "status": "pending", "priority": "low"},
            {"content": "", "status": "pending", "priority": "low"},  # empty content
            "not-a-dict",  # wrong type
            {"status": "completed", "priority": "high"},  # missing content
        ]
    }
    items = todo.parse_todo_result(result)
    assert items == [{"content": "good", "status": "pending", "priority": "low"}]


def test_parse_todo_result_none_when_no_usable_todos():
    assert todo.parse_todo_result({}) is None
    assert todo.parse_todo_result({"todos": "not-a-list"}) is None
    assert todo.parse_todo_result("not-a-dict") is None
    assert todo.parse_todo_result({"todos": []}) == []


# ------------------------------------------------------- format / tail

def test_format_todo_state_for_model_empty_yields_empty_string():
    assert todo.format_todo_state_for_model([]) == ""


def test_format_todo_state_for_model_lists_all_items():
    todos = [
        {"content": "Fix A", "status": "completed", "priority": "high"},
        {"content": "Add B", "status": "in_progress", "priority": "medium"},
    ]
    out = todo.format_todo_state_for_model(todos)
    assert "Current session todo state (authoritative):" in out
    assert "1. [completed][high] Fix A" in out
    assert "2. [in_progress][medium] Add B" in out


def test_todo_state_message_is_delimited_user_message():
    todos = [{"content": "Fix A", "status": "pending", "priority": "low"}]
    msg = todo.todo_state_message(todos)
    assert msg["role"] == "user"
    assert "<todo_list>" in msg["content"]
    assert "Fix A" in msg["content"]
    # rides as a user message (one system row allowed), never system
    assert "<todo_list>" in msg["content"]
    assert msg["content"].endswith("</untrusted_objective>")


def test_todo_state_message_empty_yields_none():
    assert todo.todo_state_message([]) is None
    assert todo.todo_state_message(None) is None


# ------------------------------------------------------- DB migration

@pytest.mark.asyncio
async def test_session_todos_column_migrates_and_roundtrips():
    cid = await database.create_conversation("Todo task")
    row = await database.get_conversation(cid)
    assert "session_todos" in row
    assert row["session_todos"] is None

    items = [{"content": "Fix A", "status": "completed", "priority": "high"}]
    await database.update_conversation(
        cid, touch_updated_at=False, session_todos=json.dumps(items)
    )
    row = await database.get_conversation(cid)
    assert json.loads(row["session_todos"]) == items

    # Empty clears it (the loop stores None for an empty checklist).
    await database.update_conversation(
        cid, touch_updated_at=False, session_todos=""
    )
    row = await database.get_conversation(cid)
    assert row["session_todos"] is None


# ------------------------------------------------------- executors

@pytest.mark.asyncio
async def test_todo_write_executor_persists_and_returns_summary():
    cid = await database.create_conversation("Todo write test")
    items = [
        {"content": "Fix A", "status": "completed", "priority": "high"},
        {"content": "Add B", "status": "in_progress", "priority": "medium"},
        {"content": "Test C", "status": "pending", "priority": "low"},
    ]
    result = await tools_mod.EXECUTORS[todo.TODO_WRITE_TOOL](
        workspace=".", conversation_id=cid, todos=items
    )
    assert result["todos"] == items
    assert result["summary"]["total"] == 3
    assert result["summary"]["completed"] == 1
    assert result["summary"]["in_progress"] == 1
    assert result["summary"]["pending"] == 1

    # It persisted to the conversation row.
    row = await database.get_conversation(cid)
    assert json.loads(row["session_todos"]) == items


@pytest.mark.asyncio
async def test_todo_write_executor_rejects_bad_input():
    result = await tools_mod.EXECUTORS[todo.TODO_WRITE_TOOL](
        workspace=".", conversation_id=1, todos="not-a-list"
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_todo_read_executor_returns_persisted_items():
    cid = await database.create_conversation("Todo read test")
    items = [{"content": "Fix A", "status": "completed", "priority": "high"}]
    await database.update_conversation(
        cid, touch_updated_at=False, session_todos=json.dumps(items)
    )
    result = await tools_mod.EXECUTORS[todo.TODO_READ_TOOL](
        workspace=".", conversation_id=cid
    )
    assert result["todos"] == items


@pytest.mark.asyncio
async def test_todo_read_executor_empty_yields_empty_list():
    cid = await database.create_conversation("Todo read empty test")
    result = await tools_mod.EXECUTORS[todo.TODO_READ_TOOL](
        workspace=".", conversation_id=cid
    )
    assert result["todos"] == []
