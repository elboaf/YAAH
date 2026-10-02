"""Issue #202: auto-generated chat titles must distill the user's request.

The title prompt quotes/fences the first user message instead of passing it
as a bare user turn, conversational model replies (refusals, answers) are
retried once and otherwise rejected, markdown emphasis is stripped, and
truncation happens at a word boundary.
"""

import asyncio

import pytest

from backend.agent import loop


def _chat_factory(responses):
    calls = []

    def fake_chat(messages, tools=None, stream=False, **kwargs):
        calls.append([dict(m) for m in messages])
        idx = min(len(calls) - 1, len(responses) - 1)
        content = responses[idx]

        async def _once():
            return {"choices": [{"message": {"content": content}}]}

        return _once()

    return fake_chat, calls


def _setup_conversation(monkeypatch, title="New chat"):
    from backend.db import database as db  # noqa: F401 — import exercised for parity

    async def fake_get_conversation(cid):
        return {"chat_type": "chat", "title": title}

    async def fake_get_messages(cid):
        return [{"role": "user", "content": "open a new gh issue in elboaf/YAAH"}]

    updates = []

    async def fake_update_conversation(cid, title=None, **kw):
        updates.append(title)

    monkeypatch.setattr(loop, "get_conversation", fake_get_conversation)
    monkeypatch.setattr(loop, "get_messages", fake_get_messages)
    monkeypatch.setattr(loop, "update_conversation", fake_update_conversation)
    return updates


def test_title_prompt_quotes_first_message(monkeypatch):
    """The user message arrives fenced as quoted material, not a live turn."""
    fake_chat, calls = _chat_factory(["Open GH Issue in Repo"])
    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    _setup_conversation(monkeypatch)

    title = asyncio.run(loop._generate_conversation_title(1, "x"))
    assert title == "Open GH Issue in Repo"
    user_content = calls[0][1]["content"]
    assert '"""' in user_content
    assert "open a new gh issue in elboaf/YAAH" in user_content


def test_imperative_message_gets_distilled_title(monkeypatch):
    """A compliant reply becomes the title (happy path)."""
    fake_chat, _ = _chat_factory(["Open GitHub Issue"])
    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    updates = _setup_conversation(monkeypatch)

    title = asyncio.run(loop._generate_conversation_title(1, "x"))
    assert title == "Open GitHub Issue"
    assert updates == ["Open GitHub Issue"]


def test_conversational_reply_retries_then_rejects(monkeypatch):
    """A refusal triggers exactly one retry; if still conversational,
    the existing title is left untouched (None)."""
    fake_chat, calls = _chat_factory(
        ["I can't open GitHub issues directly", "Happy to help with that!"]
    )
    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    updates = _setup_conversation(monkeypatch)

    title = asyncio.run(loop._generate_conversation_title(1, "x"))
    assert title is None
    assert len(calls) == 2, "exactly one retry"
    assert updates == []


def test_conversational_reply_retry_can_succeed(monkeypatch):
    fake_chat, _ = _chat_factory(
        ["I can't open GitHub issues directly", "Open GitHub Issue"]
    )
    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    updates = _setup_conversation(monkeypatch)

    title = asyncio.run(loop._generate_conversation_title(1, "x"))
    assert title == "Open GitHub Issue"
    assert updates == ["Open GitHub Issue"]


def test_markdown_emphasis_stripped(monkeypatch):
    fake_chat, _ = _chat_factory(["**Reading handoff file**"])
    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    _setup_conversation(monkeypatch)

    title = asyncio.run(loop._generate_conversation_title(1, "x"))
    assert title == "Reading handoff file"
    assert "*" not in title


def test_truncation_at_word_boundary(monkeypatch):
    long_title = (
        "one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty"
    )
    fake_chat, _ = _chat_factory([long_title])
    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    _setup_conversation(monkeypatch)

    title = asyncio.run(loop._generate_conversation_title(1, "x"))
    assert len(title) <= loop.AUTO_TITLE_MAX_CHARS
    # No mid-word slice: the truncation point must be whitespace (or clean end).
    source = " ".join(long_title.split())
    assert source.startswith(title)
    assert source[len(title):len(title) + 1] in ("", " ")


@pytest.mark.parametrize(
    "chat_type,current", [("agent", "New chat"), ("chat", "My manual title")]
)
def test_manual_titles_and_agent_chats_never_touched(monkeypatch, chat_type, current):
    """Existing guards preserved: no model call, no update."""
    calls_holder = {}

    def fake_chat(messages, tools=None, stream=False, **kwargs):
        calls_holder["hit"] = True

        async def _once():
            return {"choices": [{"message": {"content": "nope"}}]}

        return _once()

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    updates = _setup_conversation(
        monkeypatch, title=current if chat_type == "chat" else "New chat"
    )
    if chat_type == "agent":
        # override chat_type
        async def fake_get_conversation(cid):
            return {"chat_type": "agent", "title": "New chat"}

        monkeypatch.setattr(loop, "get_conversation", fake_get_conversation)

    title = asyncio.run(loop._generate_conversation_title(1, "x"))
    assert title is None
    assert "hit" not in calls_holder
    assert updates == []
