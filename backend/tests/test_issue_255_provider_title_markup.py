# Issue #255: provider-injected `<system_*>` control text (the model
# provider's low-context warning) must never reach the title channel. The
# backend auto-title compares the stored title against the first user
# message's 40-char slice; both sides must be sanitized, so a provider
# warning riding at the front of the first message neither sticks nor
# blocks the model title from replacing it.

import json

import pytest

from backend.agent import loop


class FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


async def collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


@pytest.fixture
def fake_model(monkeypatch):
    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


@pytest.mark.asyncio
async def test_provider_warning_in_first_message_does_not_block_auto_title(
    fake_model, tmp_path, monkeypatch
):
    """A first message beginning with provider `<system_warning>` markup is
    stripped before the slice compare, so the model title still fires — and
    the markup itself is never fed to the title model."""
    from backend.db.database import create_conversation, get_conversation

    warning = "<system_warning>⚠️ CONTEXT LOW - Prioritize completing current tasks</system_warning>"
    prompt = f"{warning} help me fix the flaky test suite"
    cid = await create_conversation(prompt[:40])

    async def routed(messages, tools=None, stream=True):
        if not stream:
            assert "system_warning" not in (messages[-1].get("content") or ""), (
                "provider markup must not be fed to the title model"
            )
            return {"choices": [{"message": {"content": "Flaky Test Suite Triage"}}]}
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", routed)

    events = await collect(loop.run_agent(cid, prompt, str(tmp_path)))
    title_events = [e for e in events if e["type"] == "title"]
    assert title_events and title_events[0]["title"] == "Flaky Test Suite Triage"
    conv = await get_conversation(cid)
    assert conv["title"] == "Flaky Test Suite Triage"


def test_strip_provider_markup_backend_helper():
    """The backend mirror of the frontend strip rule: leading
    `<system_*>…</system_*>` blocks are removed; an unclosed tag consumes
    the rest; ordinary text passes through."""
    strip = loop._strip_provider_markup
    assert (
        strip("<system_warning>⚠️ CONTEXT LOW</system_warning> fix the bug")
        == "fix the bug"
    )
    assert strip("<system_warning>unclosed") == ""
    assert strip("plain request") == "plain request"
    assert strip("what does <system_prompt> mean?") == "what does <system_prompt> mean?"
