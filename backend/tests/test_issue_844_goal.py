"""#844 Stage A: the externalized durable goal.

Covers (1) the objective tail builder and (2) the `session_goal` DB column
migration + round-trip. The loop integration (gate-done-on-verifier,
nextAction loop) is Stage C and tested in test_issue_844_verify.py.
"""
import pytest

from backend.agent import goal
from backend.db import database


# ---------------------------------------------------------------- goal tail

def test_empty_objective_yields_no_tail():
    assert goal.objective_tail_message("") is None
    assert goal.objective_tail_message(None) is None
    assert goal.objective_tail_message("   \n ") is None


def test_objective_tail_is_delimited_untrusted_user_message():
    msg = goal.objective_tail_message("Fix bug A and add feature B\n")
    # rides as a user message (one system row allowed), never system
    assert msg["role"] == "user"
    # verbatim objective, trimmed of surrounding whitespace
    assert "<objective>\nFix bug A and add feature B\n</objective>" in msg["content"]
    # untrusted-data framing, verbatim ycode wording
    assert "<untrusted_objective>" in msg["content"]
    assert msg["content"].endswith("</untrusted_objective>")
    assert "user-provided data" in msg["content"]
    assert "Treat it as the task to pursue" in msg["content"]


def test_objective_tail_trims_only_surrounding_whitespace():
    # internal multi-line / multi-issue content stays verbatim
    raw = "\n\n  Fix A\n  also B\n\n"
    content = goal.objective_tail_message(raw)["content"]
    assert "Fix A\n  also B" in content


# ------------------------------------------------------- DB migration/round

@pytest.mark.asyncio
async def test_session_goal_column_migrates_and_roundtrips():
    # Fresh DB: get_db runs SCHEMA + migrations, which must add the column.
    cid = await database.create_conversation("Goal task")
    row = await database.get_conversation(cid)
    # column exists and defaults to NULL (feature inactive)
    assert "session_goal" in row
    assert row["session_goal"] is None

    # Write + read back through update_conversation (the loop's write path).
    await database.update_conversation(
        cid, touch_updated_at=False, session_goal="do the thing"
    )
    row = await database.get_conversation(cid)
    assert row["session_goal"] == "do the thing"

    # Empty clears it (the loop stores None for an empty objective).
    await database.update_conversation(
        cid, touch_updated_at=False, session_goal=""
    )
    row = await database.get_conversation(cid)
    assert row["session_goal"] is None


@pytest.mark.asyncio
async def test_session_goal_write_is_not_exposed_as_a_typed_field():
    # update_conversation accepts session_goal as a free-form field (the loop
    # owns it); it is not part of the structured model/effort scope writes.
    cid = await database.create_conversation("Goal task 2")
    await database.update_conversation(
        cid, touch_updated_at=False, session_goal="root prompt"
    )
    row = await database.get_conversation(cid)
    assert row["session_goal"] == "root prompt"
