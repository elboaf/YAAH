"""Issue #290 part 2: deterministic run-worktree namespace in the prompt
contract.

The run SOP is a prompt contract until the harness materializes chat
worktrees itself: `.scratch/chat-<id>/run` on branch `run/chat-<id>`,
with the residue protocol at run start (clean+merged auto-clears; dirty
or unmerged is surfaced, never silently deleted, never silently
blocking). The surface that carries it: the branch-selector note shim (#286) —
fully injected, workspace-agnostic; the workspace AGENTS.md deliberately
does NOT restate it (#322: the shipped prompt is the single source, any
project's AGENTS.md only adds project-specific deltas).
"""
from pathlib import Path

import pytest

from backend.agent import loop


def test_note_uses_deterministic_chat_namespace():
    note = loop._selected_branch_note("bigtest", 7)
    assert ".scratch/chat-7/run" in note
    assert "run/chat-7" in note
    assert "bigtest" in note  # the landing target is named
    # Old ad-hoc naming is gone from the contract.
    assert "run-<date>-<slug>" not in note
    assert "run-YYYYMMDD" not in note


def test_note_carries_residue_protocol():
    note = loop._selected_branch_note("bigtest", 7)
    # Clean + merged residue may auto-clear; dirty/unmerged is surfaced
    # for the user to land or scrap — never silently deleted.
    assert "land it" in note or "scrap it" in note
    assert "never silently deleted" in note.lower() or "never silently" in note.lower()


def test_note_without_chat_id_renders_placeholder():
    note = loop._selected_branch_note("bigtest")
    assert ".scratch/chat-" in note  # placeholder id
    assert "bigtest" in note


def test_note_empty_when_unset():
    assert loop._selected_branch_note("") == ""
    assert loop._selected_branch_note(None, 5) == ""


@pytest.mark.asyncio
async def test_run_injects_deterministic_paths(tmp_path, monkeypatch):
    """The assembled system prompt names this chat's own run paths."""
    from backend.db.database import create_conversation, update_conversation

    async def fake_capture(cid):
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

    monkeypatch.setattr(loop, "_agents_notes", lambda workspace: "")
    cid = await create_conversation("t")
    await update_conversation(cid, selected_branch="bigtest")

    system = await fake_capture(cid)
    assert f".scratch/chat-{cid}/run" in system
    assert "bigtest" in system


def test_agents_md_does_not_restate_the_run_sop():
    """#322: the harness's injected SOP is the single, workspace-agnostic
    source (the #290 criterion prescribing AGENTS.md adoption is revised
    away with the #277 graduation that moved the SOP into the injected
    note). A workspace AGENTS.md adds only project-specific deltas; the
    deterministic namespace and branch grammar live in loop.py alone."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert ".scratch/chat-<id>/run" not in text
    assert "run/<title-slug>-<chat-id>" not in text
    assert "run/chat-<id>" not in text
    assert "run-YYYYMMDD" not in text
    assert "land it" not in text and "scrap it" not in text


def test_notes_are_workspace_agnostic():
    """#322: no note variant may lean on a workspace AGENTS.md rule or any
    other project-specific file — the SOP is self-contained everywhere,
    YAAH or not. (Regression pin for the dangling 'interim master-landing
    rule' reference in the degraded variant.)"""
    for note in (
        loop._selected_branch_note("bigtest", 7, title="Fix TTS whine"),
        loop._selected_branch_note("bigtest", 7, detached=True),
        loop._selected_branch_note_degraded("bigtest", 7, "non-repo", title="Fix TTS whine"),
        loop._selected_branch_note_stale("bigtest", 7),
    ):
        assert "AGENTS.md" not in note
        assert "interim" not in note
