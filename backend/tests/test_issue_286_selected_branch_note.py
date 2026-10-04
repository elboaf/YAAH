"""Branch-selector pick reaches the model (issue #286 follow-up).

The per-chat selected_branch (issue #286, slice 1) is stored on the
conversation row but is otherwise invisible to the model — the run loop
never showed it. These tests pin the shim: when a pick is stored, the
run's system prompt carries a note that names the branch and states the
intent-only contract (primary tree never moves); with no pick, no note.
"""

import pytest

from backend.agent import loop
from backend.db.database import (
    create_conversation,
    get_conversation,
    update_conversation,
)


def test_note_names_branch_and_states_intent_only_contract():
    note = loop._selected_branch_note("bigtest")
    assert "# Branch selector: bigtest" in note
    assert "bigtest" in note
    # The promise that survived #277: the primary tree is the human's.
    assert "primary" in note
    assert "never" in note.lower()


@pytest.mark.parametrize("branch", ["", None, "   "])
def test_note_empty_when_unset(branch):
    assert loop._selected_branch_note(branch) == ""


async def _capture_system_prompt(cid, tmp_path):
    captured = {}

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        captured["system"] = messages[0]["content"]

        async def _stream():
            yield {"type": "content", "text": "done"}
            yield {"type": "finish"}
        return _stream()

    orig_chat = loop.model_client.chat
    loop.model_client.chat = fake_chat
    try:
        async for _ in loop.run_agent(cid, "go", str(tmp_path)):
            pass
    finally:
        loop.model_client.chat = orig_chat
    return captured["system"]


@pytest.mark.asyncio
async def test_run_injects_selected_branch_note(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "_agents_notes", lambda workspace: "")
    cid = await create_conversation("t")
    await update_conversation(cid, selected_branch="bigtest")
    # The storage plumbing the note reads from actually persisted the pick.
    assert (await get_conversation(cid))["selected_branch"] == "bigtest"

    system = await _capture_system_prompt(cid, tmp_path)
    assert "# Branch selector: bigtest" in system
    assert "per-chat worktree" in system
    assert "never check it out" in system


@pytest.mark.asyncio
async def test_run_omits_note_without_pick(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "_agents_notes", lambda workspace: "")
    cid = await create_conversation("t")

    system = await _capture_system_prompt(cid, tmp_path)
    assert "# Branch selector:" not in system
