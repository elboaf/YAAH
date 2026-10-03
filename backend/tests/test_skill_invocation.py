"""Regression tests for invoked-skill prompt injection.

The user's symptom: starting a chat with a skill chip (e.g. /ask-matt) shows
the chip, but the agent never has the skill loaded. This test drives
run_agent with skill_names and captures what the model actually receives.
"""
import asyncio
import json

import pytest

from backend.agent import loop, skills as skill_registry
from backend.tests.test_agent import fake_model, collect  # noqa: F401


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(skill_registry, "SKILLS_DIR", d)
    monkeypatch.setattr(skill_registry, "_scanned", False)
    dd = d / "ask-matt"
    dd.mkdir()
    (dd / "SKILL.md").write_text(
        "---\nname: ask-matt\ndescription: router\n---\n\nROUTER-BODY-MARKER\n",
        encoding="utf-8",
    )
    return d


@pytest.mark.asyncio
async def test_invoked_skill_reaches_model_context(fake_model, skills_dir, tmp_path, monkeypatch):
    """run_agent(skill_names=["ask-matt"]) must inject the skill body into
    the system prompt the model sees."""
    from backend.db.database import create_conversation

    captured: list[list] = []

    async def fake_chat(messages, tools=None, stream=True):
        captured.append(messages)
        from backend.tests.test_agent import FakeStream

        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    monkeypatch.setattr(loop, "_generate_conversation_title", lambda *_: asyncio.sleep(0, result=None))

    cid = await create_conversation("skill-invoke")
    events = await collect(loop.run_agent(cid, "help me route", str(tmp_path), skill_names=["ask-matt"]))

    assert events[-1]["type"] == "done"
    assert captured, "model never called"
    sys = captured[0][0]["content"]
    assert "ROUTER-BODY-MARKER" in sys, "invoked skill body NOT in model system prompt"
    assert "# Invoked skills" in sys


@pytest.mark.asyncio
async def test_unknown_skill_emits_skill_not_found_event(fake_model, skills_dir, tmp_path, monkeypatch):
    """An invoked skill the registry doesn't know must surface as a visible
    stream event, not a silent prompt injection."""
    from backend.db.database import create_conversation

    async def fake_chat(messages, tools=None, stream=True):
        from backend.tests.test_agent import FakeStream
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    monkeypatch.setattr(loop, "_generate_conversation_title", lambda *_: asyncio.sleep(0, result=None))

    cid = await create_conversation("skill-invoke-404")
    events = await collect(loop.run_agent(cid, "go", str(tmp_path), skill_names=["nope-matt"]))
    ev = next((e for e in events if e["type"] == "skill_not_found"), None)
    assert ev is not None, "skill_not_found event missing for unknown skill"
    assert ev["skills"] == ["nope-matt"]

@pytest.mark.asyncio
async def test_unknown_skill_in_authoritative_block_is_excluded(
    fake_model, skills_dir, tmp_path, monkeypatch
):
    """#193: the authoritative "# Invoked skills" block must never contain a
    "# Skill not found" entry — one block cannot both order the model to obey
    its content and tell it the content does not exist. Unknown names are
    reported via the skill_not_found event only."""
    from backend.db.database import create_conversation

    captured: list[list] = []

    async def fake_chat(messages, tools=None, stream=True):
        captured.append(messages)
        from backend.tests.test_agent import FakeStream

        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    monkeypatch.setattr(loop, "_generate_conversation_title", lambda *_: asyncio.sleep(0, result=None))

    cid = await create_conversation("skill-invoke-mixed")
    events = await collect(
        loop.run_agent(cid, "go", str(tmp_path), skill_names=["ask-matt", "nope-matt"])
    )
    sys = captured[0][0]["content"]
    block = sys[sys.index("# Invoked skills"):]
    assert "# Skill not found" not in block, "not-found heading inside the authoritative block"
    assert "ROUTER-BODY-MARKER" in block, "known skill body must stay in the block"
    ev = next((e for e in events if e["type"] == "skill_not_found"), None)
    assert ev is not None and ev["skills"] == ["nope-matt"]


@pytest.mark.asyncio
async def test_queued_unknown_skill_emits_skill_not_found_event(
    fake_model, skills_dir, tmp_path, monkeypatch
):
    """#193: a queued/steer message with a typo'd skill chip must produce the
    same user-visible skill_not_found event as the turn path — not a silent
    "# Skill not found" heading injected into model context."""
    from backend.db.database import create_conversation

    cid = await create_conversation("skill-queue-404")

    scripts = fake_model
    scripts.append(
        [
            {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": json.dumps({"url": "about:blank"})},
                    }
                ],
            },
            {"type": "finish"},
        ]
    )
    scripts.append([{"type": "content", "text": "b"}, {"type": "finish"}])

    real_fake_chat = loop.model_client.chat

    async def fake_chat(messages, tools=None, stream=True):
        if not getattr(fake_chat, "queued", False):
            fake_chat.queued = True
            loop.enqueue_message(cid, "follow up", skills=["nope-matt"])
        return await real_fake_chat(messages, tools=tools, stream=stream)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    events = await collect(loop.run_agent(cid, "go", str(tmp_path)))
    ev = next((e for e in events if e["type"] == "skill_not_found"), None)
    assert ev is not None, "queued-path unknown skill must emit skill_not_found"
    assert ev["skills"] == ["nope-matt"]


@pytest.mark.asyncio
async def test_unknown_name_reported_per_message_even_when_repeated(
    fake_model, skills_dir, tmp_path, monkeypatch
):
    """CodeRabbit return trip on PR #252: the unknown-name dedupe set
    (loaded_skills) must only carry KNOWN names. If the turn already invoked
    `nope-matt` (event #1) and a queued message selects `nope-matt` again,
    the queued message still gets its own skill_not_found event (#2) —
    every message selecting an unknown skill reports it."""
    from backend.db.database import create_conversation

    cid = await create_conversation("skill-queue-404-repeat")

    scripts = fake_model
    scripts.append(
        [
            {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": json.dumps({"url": "about:blank"})},
                    }
                ],
            },
            {"type": "finish"},
        ]
    )
    scripts.append([{"type": "content", "text": "b"}, {"type": "finish"}])

    real_fake_chat = loop.model_client.chat

    async def fake_chat(messages, tools=None, stream=True):
        if not getattr(fake_chat, "queued", False):
            fake_chat.queued = True
            loop.enqueue_message(cid, "follow up", skills=["nope-matt"])
        return await real_fake_chat(messages, tools=tools, stream=stream)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    events = await collect(loop.run_agent(cid, "go", str(tmp_path), skill_names=["nope-matt"]))
    found = [e for e in events if e["type"] == "skill_not_found"]
    assert len(found) == 2, (
        f"expected one skill_not_found per selecting message (turn + queued), got {len(found)}: "
        + repr([e.get("skills") for e in found])
    )
    assert all(e["skills"] == ["nope-matt"] for e in found)
