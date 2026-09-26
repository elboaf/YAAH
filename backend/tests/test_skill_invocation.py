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
