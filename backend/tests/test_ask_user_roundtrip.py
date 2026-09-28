"""Regression: the ask_user roundtrip must keep every emission visible.

Backend half of the 2026-09-30 vanish-after-answer report: the loop's stream
and the persisted transcript are verified to carry all emissions through an
answered ask_user (the disappearance was frontend render, but this pins the
backend contract the UI relies on).

User symptom (2026-09-30): in an interactive chat, a run's text emissions
rendered fine until the agent called ask_user; after the user answered, the
run's chat text vanished from the live view and only the tool-call trace
remained. The conversation EXPORT still contained every emission — so the
question is whether the backend stream ever emits the text, or the frontend
buffer loses it.

This loop replays the exact shape against the REAL backend loop with a
scripted model: text -> tools -> text -> ask_user -> (answer) -> text -> done,
and asserts every emission arrives as a `text` stream event.
"""
import asyncio
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


@pytest.fixture
def fake_model(monkeypatch):
    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


async def collect(agent_gen):
    out = []
    async for e in agent_gen:
        out.append(json.loads(e))
    return out


@pytest.mark.asyncio
async def test_emissions_stream_and_persist_through_an_ask_user_roundtrip(fake_model, tmp_path):
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("ask-emission repro")

    # Turn shape mirroring the incident: preamble text, a couple of tool
    # rounds, then an ask_user, then post-answer text and tools, then done.
    fake_model.append(
        [
            {"type": "content", "text": "I'll write the report to the temp dir via shell."},
            {"type": "tool_calls", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})}},
            ]},
            {"type": "finish"},
        ]
    )
    fake_model.append(
        [
            {"type": "content", "text": "Need your call on how to test this."},
            {"type": "tool_calls", "tool_calls": [
                {"id": "q1", "type": "function",
                 "function": {"name": "ask_user", "arguments": json.dumps(
                     {"question": "How to test?", "options": [{"label": "VM harness"}, {"label": "Myself"}]})}},
            ]},
            {"type": "finish"},
        ]
    )
    fake_model.append(
        [
            {"type": "content", "text": "Sounds good - it's all yours. Everything is committed on master."},
            {"type": "finish"},
        ]
    )

    async def answer_soon():
        # Wait until the run is actually blocked on the question (the
        # pending-answer future exists), then answer like POST /answer does.
        for _ in range(500):
            if f"{cid}:q1" in loop._pending_answers:
                break
            await asyncio.sleep(0.01)
        assert f"{cid}:q1" in loop._pending_answers, "run never registered the question"
        assert loop.resolve_answer(cid, "q1", "I'll test it myself")

    events, _ = await asyncio.gather(
        collect(loop.run_agent(cid, "go", str(tmp_path))),
        answer_soon(),
    )

    texts = [e.get("text", "") for e in events if e["type"] == "text"]
    joined = "\n".join(texts)
    assert "I'll write the report to the temp dir via shell." in joined, texts
    assert "Need your call on how to test this." in joined, texts
    assert "Sounds good - it's all yours." in joined, texts

    # And the persisted transcript must carry all three emissions.
    rows = await get_messages(cid)
    assistant_text = "\n".join(
        r["content"] or "" for r in rows if r["role"] == "assistant"
    )
    assert "I'll write the report" in assistant_text
    assert "Need your call" in assistant_text
    assert "Sounds good" in assistant_text
