"""False 'agent run failed' toasts — three regression tests:

1. A provider glitch streaming a tool call with an empty id/name must not
   produce a persistable tool call (it used to poison the conversation and
   400 every later turn: "tool messages must include a non-empty string
   tool_call_id").
2. History rebuild must skip tool rows with an empty tool_call_id (already
   persisted by older builds) instead of replaying them.
3. A rate-limited (429/quota) fire that still has retries left settles as
   'error_quiet' — the toast watcher only fires on exact 'error' — so one
   quota outage doesn't toast once per retry fire.
"""
import json
from datetime import datetime

import httpx
import pytest

from backend.agent import loop
from backend.agent import model_client
from backend.agent import scheduler as sched
from backend.db.database import (
    add_message,
    create_agent,
    create_conversation,
    get_agent,
    get_messages,
)


def sse(lines):
    body = "".join(f"data: {json.dumps(l)}\n\n" for l in lines) + "data: [DONE]\n\n"
    return body.encode("utf-8")


# ------------------------------------------------- empty tool call streaming

@pytest.mark.asyncio
async def test_stream_drops_degenerate_tool_call(monkeypatch, tmp_path):
    """A streamed slot that never received an id or a name is dropped; the
    valid call in the same emission survives."""
    def handler(request):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "", "function": {"name": "", "arguments": ""}},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_ok", "function": {"name": "bash", "arguments": "{}"}},
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        return httpx.Response(200, content=sse(chunks))

    real_client = httpx.AsyncClient
    def factory(**kw):
        kw.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(model_client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(model_client, "load_config", lambda: {
        "providers": {"p": {"api_base": "http://x", "api_key": "k", "model": "m"}},
        "active_provider": "p", "api_base": "http://x", "api_key": "k", "model": "m",
    })

    stream = await model_client.chat([{"role": "user", "content": "hi"}], stream=True)
    events = [e async for e in stream]
    tc_events = [e for e in events if e["type"] == "tool_calls"]
    assert len(tc_events) == 1
    assert tc_events[0]["tool_calls"] == [{
        "id": "call_ok", "type": "function",
        "function": {"name": "bash", "arguments": "{}"},
    }]


@pytest.mark.asyncio
async def test_stream_all_degenerate_becomes_no_tool_call(monkeypatch, tmp_path):
    """An emission that is ONLY degenerate slots becomes a plain reply —
    never a tool_calls event the loop would persist."""
    def handler(request):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "", "arguments": ""}},
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        return httpx.Response(200, content=sse(chunks))

    real_client = httpx.AsyncClient
    def factory(**kw):
        kw.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(model_client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(model_client, "load_config", lambda: {
        "providers": {"p": {"api_base": "http://x", "api_key": "k", "model": "m"}},
        "active_provider": "p", "api_base": "http://x", "api_key": "k", "model": "m",
    })

    stream = await model_client.chat([{"role": "user", "content": "hi"}], stream=True)
    events = [e async for e in stream]
    assert not [e for e in events if e["type"] == "tool_calls"]


# ------------------------------------------------------- poisoned history

@pytest.mark.asyncio
async def test_history_rebuild_skips_empty_tool_call_id(monkeypatch, tmp_path):
    """A conversation poisoned by an older build (assistant tool_calls with
    empty id + tool row with empty tool_call_id) replays clean: no tool
    message with an empty tool_call_id reaches the model."""
    class FakeStream:
        def __init__(self, events):
            self._events = list(events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

    cid = await create_conversation("poisoned", chat_type="agent")
    await add_message(cid, "user", "do the thing")
    await add_message(
        cid, "assistant", "",
        tool_calls=[{"id": "", "type": "function", "function": {"name": "", "arguments": ""}}],
    )
    await add_message(
        cid, "tool", json.dumps({"error": "Unknown tool: ."}), tool_call_id="",
    )
    await add_message(cid, "user", "continue")

    seen = {}

    async def spy_chat(messages, tools=None, stream=True, model="", effort=""):
        seen["messages"] = messages
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", spy_chat)
    async for _ in loop.run_agent(cid, "again", str(tmp_path)):
        pass

    replayed = seen["messages"]
    tool_msgs = [m for m in replayed if m.get("role") == "tool"]
    assert all(m.get("tool_call_id") for m in tool_msgs), (
        f"empty tool_call_id replayed to the model: {tool_msgs}")
    # The malformed assistant call (id="") must not appear either.
    for m in replayed:
        for tc in m.get("tool_calls") or []:
            assert tc.get("id"), "malformed tool call replayed to the model"


# -------------------------------------------------- quiet rate-limit retries

@pytest.mark.asyncio
async def test_rate_limited_retry_is_quiet(monkeypatch):
    """A 429/quota fire with retries remaining settles 'error_quiet' (no
    toast — the watcher only fires on exact 'error'); a non-rate-limit
    failure still settles 'error'."""
    from backend.tests.test_scheduler import make_agent, drain_pending

    conv = await create_conversation("agent chat", chat_type="agent")
    agent_row = await create_agent(make_agent(
        conversation_id=conv,
        next_fire_at=datetime.now().isoformat(timespec="seconds"),
    ))

    calls = {"n": 0}

    async def fake_run(cid, prompt, workspace, **kw):
        calls["n"] += 1
        raise RuntimeError("Model API error 429: Weekly/Monthly Limit Exhausted")
        yield  # pragma: no cover

    monkeypatch.setattr(loop, "run_agent", fake_run)
    monkeypatch.setattr(sched, "get_retry_settings", lambda: (2, 5))

    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent) == "started"
    from backend.tests.test_scheduler import drain_pending
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "error_quiet", "rate-limited retry must not toast"

    # Non-rate-limit failures keep the loud status.
    async def fake_run_502(cid, prompt, workspace, **kw):
        raise RuntimeError("provider 502")
        yield  # pragma: no cover

    monkeypatch.setattr(loop, "run_agent", fake_run_502)
    agent = await get_agent(agent_row["id"])
    assert await sched.fire_agent(agent, is_retry=True) == "started"
    await drain_pending()
    row = await get_agent(agent_row["id"])
    assert row["last_status"] == "error"


def test_is_rate_limit_matches_provider_wordings():
    assert sched._is_rate_limit("Model API error 429: {quota}")
    assert sched._is_rate_limit("Weekly/Monthly Limit Exhausted")
    assert sched._is_rate_limit("Too Many Requests")
    assert sched._is_rate_limit("rate limit hit, back off")
    assert not sched._is_rate_limit("Model API error 500: oops")
    assert not sched._is_rate_limit("ConnectError:")
    assert not sched._is_rate_limit("")
