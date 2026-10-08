"""Scheduled-fire spoken briefings (issue #296) — the consumption half.

Generation and relay are already correct end to end: run_agent emits
{"type": "say", "text": ...} per briefing and the fire's _tape_append
relays it into the tape buffer with text intact (_TAPE_FIELDS carries
"text" — the maintainer triage note's correction to the issue body).
What was missing is consumption: nothing rendered or spoke say events
from the tape, and there was no per-agent fire-time speech policy.

These tests lock the backend half:

- the fire relay must keep say text on the tape (a whitelist regression
  would reintroduce #296's "bare type tag" drop — the exact bug the
  maintainer's correction ruled out, so it stays guarded);
- the per-agent say_mode setting (arrival | visible, default arrival —
  the maintainer decision recorded on the issue): column migration, API
  validation, and the editor round-trip.

The frontend consumption (tape render + speak-on-arrival watcher) is
covered by src/issue296SayFire.test.tsx.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.agent import loop
from backend.agent import scheduler as sched
from backend.db.database import (
    create_agent,
    create_conversation,
    get_agent,
)


def uuid_hex():
    return uuid.uuid4().hex[:12]


def make_agent(workspace="C:/ws", name="nightly", prompt="summarize commits", **kw):
    """Row dict shaped like the API layer's create payload (mirrors
    test_scheduler's helper)."""
    import json

    fields = {
        "id": uuid_hex(),
        "workspace": workspace,
        "name": name,
        "prompt": prompt,
        "schedule_type": "interval",
        "schedule_spec": json.dumps({"minutes": 30}),
        "approval_policy": "sandbox-only",
        "enabled": 1,
        "memory_enabled": 1,
    }
    fields.update(kw)
    return fields


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

    async def fake_chat(messages, tools=None, stream=True, model="", effort=""):
        scripts_last = scripts.pop(0) if scripts else [{"type": "finish"}]
        return FakeStream(scripts_last)

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)
    return scripts


async def drain_pending(max_wait=1.0):
    for _ in range(int(max_wait / 0.01)):
        await asyncio.sleep(0.01)


# ------------------------------------------------------------- fire relay


@pytest.mark.asyncio
async def test_fire_relays_say_text_to_tape(fake_model, tmp_path):
    """The one thing #296's reporter observed missing upstream of speech:
    a fired run's say briefing must reach the tape WITH its text. The
    maintainer triage note verified this already works; this test is the
    regression lock (a _TAPE_FIELDS whitelist drop reintroduces #296's
    'bare {"type":"say"}' silently)."""
    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        workspace=str(tmp_path), conversation_id=conv,
        prompt="summarize commits",
    ))
    fake_model.append([
        {"type": "content", "text": "Working. <say>Run finished, all green.</say>"},
        {"type": "finish"},
    ])
    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent) == "started"
    await drain_pending()

    events = sched.tape_snapshot(conv)["events"]
    says = [e for e in events if e.get("type") == "say"]
    assert says, "say event never reached the tape — #296 regressed upstream of speech"
    assert says[0].get("text"), (
        "say event lost its text on the tape — a _TAPE_FIELDS whitelist "
        "drop would make the briefing unrenderable and unspeakable"
    )


# ------------------------------------------------- per-agent say_mode rails


def _client():
    from backend.main import app

    return TestClient(app)


def _agent_body(**kw):
    base = {
        "name": "say mode",
        "prompt": "p",
        "schedule_type": "interval",
        "schedule_spec": {"minutes": 30},
    }
    base.update(kw)
    return base


def test_say_mode_defaults_to_arrival_and_round_trips():
    """The maintainer decision: per-agent setting, default arrival."""
    with _client() as c:
        r = c.post("/api/agents", json=_agent_body())
        assert r.status_code == 200
        body = r.json()
        assert body["say_mode"] == "arrival"
        aid = body["id"]
        r = c.patch(f"/api/agents/{aid}", json=_agent_body(say_mode="visible"))
        assert r.status_code == 200
        assert r.json()["say_mode"] == "visible"
        # ...and the edit survives a re-read.
        r = c.get("/api/agents")
        assert r.status_code == 200
        match = [a for a in r.json()["agents"] if a["id"] == aid]
        assert match and match[0]["say_mode"] == "visible"


def test_api_rejects_unknown_say_mode():
    with _client() as c:
        r = c.post("/api/agents", json=_agent_body(say_mode="sometimes"))
        assert r.status_code == 400
