"""Issue #312: run branches named after the work, not the chat.

The run SOP's branch name becomes ``run/<title-slug>-<chat-id>`` so
`git branch` reads like a changelog instead of a phone book. The chat
id stays in the name as the stable, collision-free suffix (titles
repeat and change mid-chat); the generic-title case falls back to
``run/chat-<id>``. The name is presentational prompt discipline: every
programmatic consumer (runwatch badge, sweep, residue) keys off the
deterministic worktree PATH, never the name.
"""
import pytest

from backend.agent import loop


# ------------------------------------------------------------- _run_branch_name


def test_branch_name_slugs_the_title_and_keeps_the_id():
    assert loop._run_branch_name("Fix TTS whine", 631) == "run/fix-tts-whine-631"


def test_branch_name_slug_rules():
    # punctuation and casing collapse into single dashes
    assert loop._run_branch_name("Sidebar: collapsed headers + runs!", 42) == (
        "run/sidebar-collapsed-headers-runs-42"
    )
    # leading/trailing junk never survives into the name
    assert loop._run_branch_name("  -- weird title --  ", 3) == "run/weird-title-3"


def test_branch_name_caps_slug_length():
    name = loop._run_branch_name("x" * 100, 5)
    slug = name.removeprefix("run/").removesuffix("-5")
    assert len(slug) == 40
    assert name == f"run/{'x' * 40}-5"


def test_branch_name_falls_back_for_generic_or_empty_titles():
    # The mechanical default title and the empty/None cases read as
    # "nothing to describe yet": keep the old deterministic name.
    for title in ("New Task", "", None, "   "):
        assert loop._run_branch_name(title, 7) == "run/chat-7"


def test_branch_name_placeholder_when_no_chat_id():
    # Template renders (docstrings, prompt-manifest prose) keep <id>.
    assert loop._run_branch_name("Fix TTS whine") == "run/fix-tts-whine-<id>"
    assert loop._run_branch_name(None) == "run/chat-<id>"


# ------------------------------------------------- note-level contract


def test_note_names_the_descriptive_branch_when_titled():
    note = loop._selected_branch_note("bigtest", 7, title="Fix TTS whine")
    assert "run/fix-tts-whine-7" in note
    assert "run/chat-7" not in note  # no stale name left behind
    assert ".scratch/chat-7/run" in note  # path namespace unchanged
    assert "bigtest" in note  # landing target still named


def test_note_falls_back_without_a_title():
    note = loop._selected_branch_note("bigtest", 7)
    assert "run/chat-7" in note


def test_degraded_note_honors_the_title_too():
    note = loop._selected_branch_note_degraded("bigtest", 7, "non-repo", title="Fix TTS whine")
    assert "run/fix-tts-whine-7 bigtest" in note
    note = loop._selected_branch_note_degraded("bigtest", 7, "non-repo")
    assert "run/chat-7 bigtest" in note


def test_detached_note_honors_the_title():
    note = loop._selected_branch_note("bigtest", 7, detached=True, title="Fix TTS whine")
    assert "run/fix-tts-whine-7" in note
    assert "run/chat-7" not in note


@pytest.mark.parametrize("title", ["Ünïcode Tïtle", "日本語"], ids=["latin", "cjk"])
def test_non_ascii_titles_slug_to_something_valid(title):
    name = loop._run_branch_name(title, 9)
    assert name.startswith("run/")
    assert name.endswith("-9")
    slug = name[4:-2]
    assert all(c.isalnum() and c.isascii() for c in slug.split("-") if c)


# ------------------------------------------------- end-to-end injection


@pytest.mark.asyncio
async def test_run_injects_descriptive_branch_name(tmp_path, monkeypatch):
    """The assembled system prompt names the run branch after the work."""
    from backend.db.database import create_conversation, update_conversation

    captured = {}

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        captured["system"] = messages[0]["content"]

        async def _stream():
            yield {"type": "content", "text": "done"}
            yield {"type": "finish"}
        return _stream()

    async def no_title(*_args):
        return None

    monkeypatch.setattr(loop, "_agents_notes", lambda workspace: "")
    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    monkeypatch.setattr(loop, "_generate_conversation_title", no_title)

    cid = await create_conversation("Fix TTS whine")
    await update_conversation(cid, selected_branch="bigtest")

    async for _ in loop.run_agent(cid, "go", str(tmp_path)):
        pass

    assert f"run/fix-tts-whine-{cid}" in captured["system"]
    assert f"run/chat-{cid}" not in captured["system"]
