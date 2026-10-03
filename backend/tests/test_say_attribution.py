# Issue #230: export attribution + raw-briefing persistence. What reaches
# `messages.say` must be the MODEL's `<say>` briefing when one was emitted,
# with the speech normalization (spoken_line) applied only at speak-time —
# and the export must be able to tell a model briefing from a heuristic
# fallback. Run: pytest backend/tests/test_say_attribution.py
import pytest


@pytest.fixture
def fake_model(monkeypatch):
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

    scripts: list[list[dict]] = []

    async def fake_chat(messages, tools=None, stream=True):
        events = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(events)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


async def _collect(gen):
    import json

    out = []
    async for ev in gen:
        out.append(json.loads(ev))
    return out


# ---- Item 1a: the model briefing persists verbatim ----------------------------

@pytest.mark.asyncio
async def test_model_say_persists_verbatim_not_normalized(fake_model, tmp_path):
    """#230: the raw model briefing persists, not the spoken_line()
    normalization (spelled-out numbers, flattened markdown). The model's
    `1,234` must survive into `messages.say`; normalization belongs at
    speak-time."""
    from backend.agent import loop
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("say-verbatim-230")
    fake_model.append([
        {"type": "content", "text": "Visible. <say>Fixed 1,234 lines. **Bold** claim.</say>"},
        {"type": "finish"},
    ])
    events = await _collect(loop.run_agent(cid, "hi", str(tmp_path)))

    said = [e for e in events if e["type"] == "say"]
    assert said, "a say event must still be emitted for live TTS"
    # The event stays normalized (TTS input)... but the row keeps the raw text.
    msgs = await get_messages(cid)
    assert msgs[-1]["say"] == "Fixed 1,234 lines. **Bold** claim."


@pytest.mark.asyncio
async def test_say_event_is_normalized_for_speech(fake_model, tmp_path):
    """#230: normalization moved to speak-time, the wire event remains the
    TTS-ready line (numbers spelled out), so the synthesizer contract is
    unchanged."""
    from backend.agent import loop
    from backend.db.database import create_conversation

    cid = await create_conversation("say-event-230")
    fake_model.append([
        {"type": "content", "text": "Visible. <say>Fixed 1234 lines.</say>"},
        {"type": "finish"},
    ])
    events = await _collect(loop.run_agent(cid, "hi", str(tmp_path)))
    said = [e for e in events if e["type"] == "say"]
    assert said
    assert "one thousand" in said[0]["text"].lower() or "twelve" in said[0]["text"].lower()


@pytest.mark.asyncio
async def test_say_flag_false_when_heuristic_fallback_used(fake_model, tmp_path):
    """#230: a tag-less emission still speaks (heuristic fallback) but the
    row must record that this line is NOT a model-authored briefing."""
    from backend.agent import loop
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("say-fallback-230")
    fake_model.append([
        {"type": "content", "text": "Just the visible answer."},
        {"type": "finish"},
    ])
    events = await _collect(loop.run_agent(cid, "hi", str(tmp_path)))
    said = [e for e in events if e["type"] == "say"]
    assert said and said[0]["text"], "fallback still speaks"

    msgs = await get_messages(cid)
    assert msgs[-1]["say"], "fallback line still persists"
    assert msgs[-1]["say_is_fallback"] == 1


# ---- Item 1b: the export distinguishes the two --------------------------------

@pytest.mark.asyncio
async def test_export_marks_fallback_briefing(fake_model, tmp_path):
    """#230: export must let a reader tell a heuristic fallback from a model
    briefing."""
    import httpx

    from backend.db.database import add_message, create_conversation
    from backend.main import app

    cid = await create_conversation("Export Attribution 230")
    await add_message(cid, "user", "hello")
    await add_message(
        cid, "assistant", "visible", say="Derived fallback line.",
        say_is_fallback=True,
    )
    await add_message(
        cid, "assistant", "visible 2", say="Authored briefing.",
        say_is_fallback=False,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/conversations/{cid}/export")
        assert r.status_code == 200
        body = r.text
    assert "*Briefing (auto):* Derived fallback line." in body
    assert "*Briefing:* Authored briefing." in body
