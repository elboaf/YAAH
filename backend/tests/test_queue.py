"""Queued messages + steering (issue #7): server-side per-conversation
queue, step-boundary injection, steer interruption, and end-of-run rules.
The fake model (installed on the loop's own module reference, the same
pattern test_agent.py uses) drives multi-step turns so the tests can
queue mid-run, steer, and assert the event tape + persisted transcript."""

import json

import pytest

from backend.agent import loop


class _FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._wrap(self._events.pop(0))

    async def _wrap(self, e):
        return e


class FakeClient:
    """Two model steps: step 1 calls a bash tool, step 2 answers final."""

    def __init__(self, hook=None):
        self.calls = 0
        self.hook = hook  # async fn(client) run before step 1's stream opens

    async def chat(self, messages, tools=None, stream=False, **kw):
        if self.calls == 0 and self.hook:
            self.calls += 1
            await self.hook(self)
            step1 = True
        else:
            self.calls += 1
            step1 = False
        if step1:
            return _FakeStream(
                [
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "web_fetch",
                                    "arguments": json.dumps({"url": "about:blank"}),
                                },
                            }
                        ],
                    },
                    {"type": "finish"},
                ]
            )
        return _FakeStream([{"type": "content", "text": "done"}, {"type": "finish"}])


class ModelError(Exception):
    pass


# The loop does `from backend.agent import model_client` - a real module
# (backend/agent/model_client.py) whose module-level `chat` the loop calls.
# The fake replaces that module's `chat` function (and ModelError stays
# real), so both attribute lookups hit the fake.
@pytest.fixture()
def patch_model(monkeypatch):
    import backend.agent.model_client as mc

    def _install(client):
        async def fake_chat(messages, tools=None, stream=False, model="", effort=""):
            return await client.chat(messages, tools=tools, stream=stream)

        monkeypatch.setattr(mc, "chat", fake_chat)

    return _install


async def _collect(gen):
    out = []
    async for line in gen:
        out.append(json.loads(line))
    return out


@pytest.mark.asyncio
async def test_queue_drains_at_step_boundary(patch_model, tmp_path):
    """A message queued mid-run lands as a real user turn between steps:
    persisted, announced (user_injected), and appended to model context."""
    from backend.db.database import create_conversation, get_messages

    cid = await create_conversation("q7-boundary")

    async def queue_mid_run(client):
        loop.enqueue_message(cid, "check the logs too")

    patch_model(FakeClient(hook=queue_mid_run))

    events = await _collect(loop.run_agent(cid, "go", str(tmp_path)))
    types = [e["type"] for e in events]
    assert "user_injected" in types
    inj = next(e for e in events if e["type"] == "user_injected")
    assert inj["text"] == "check the logs too"
    assert types[-1] == "done"
    # Persisted transcript: the injected user row lands right after the
    # assistant emission that carried the tool call (the tool RESULT row
    # persists after it - the injection fires at the start of the result
    # handling in the current wiring), then the final assistant row.
    rows = await get_messages(cid)
    roles = [r["role"] for r in rows]
    assert roles == ["user", "assistant", "user", "tool", "assistant"]


@pytest.mark.asyncio
async def test_steer_flag_is_per_conversation(tmp_path):
    """steer_agent flips only the target conversation's flag; unknown
    conversations report False."""
    from backend.db.database import create_conversation

    cid = await create_conversation("q7-steer")
    other = await create_conversation("q7-steer-other")
    loop._cancel_events[cid] = loop.asyncio.Event()
    loop._steer_flags[cid] = loop.asyncio.Event()
    try:
        assert loop.steer_agent(cid) is True
        assert loop._steer_flags[cid].is_set()
        assert loop.steer_agent(other) is False
    finally:
        loop._cancel_events.pop(cid, None)
        loop._steer_flags.pop(cid, None)


@pytest.mark.asyncio
async def test_natural_completion_autosends_queued(patch_model, tmp_path):
    """Run ends naturally with a still-queued message: queued_autosend
    fires so the frontend can send it as a fresh turn."""
    from backend.db.database import create_conversation

    cid = await create_conversation("q7-auto")
    patch_model(FakeClient())
    loop.enqueue_message(cid, "what got done?")
    events = await _collect(loop.run_agent(cid, "go", str(tmp_path)))
    auto = [e for e in events if e["type"] == "queued_autosend"]
    assert len(auto) == 1
    assert auto[0]["items"][0]["text"] == "what got done?"


@pytest.mark.asyncio
async def test_hard_stop_holds_queue(patch_model, tmp_path):
    """A cancelled run holds the queue: no autosend, items remain queued."""
    from backend.db.database import create_conversation

    cid = await create_conversation("q7-stop")

    async def cancel_immediately(client):
        loop.cancel_agent(cid)

    patch_model(FakeClient(hook=cancel_immediately))
    loop.enqueue_message(cid, "held message")
    events = await _collect(loop.run_agent(cid, "go", str(tmp_path)))
    types = [e["type"] for e in events]
    assert "queued_autosend" not in types
    assert loop.queue_items(cid), "hard Stop must hold the queue"


@pytest.mark.asyncio
async def test_queue_shape_fifo_and_remove():
    loop._message_queues.clear()
    a = loop.enqueue_message(1, "first")
    b = loop.enqueue_message(1, "second")
    loop.enqueue_message(2, "other conv")
    assert [i["text"] for i in loop.queue_items(1)] == ["first", "second"]
    assert loop.remove_queued(1, b["id"]) is True
    assert loop.remove_queued(1, b["id"]) is False
    assert [i["text"] for i in loop.queue_items(1)] == [a["text"]]
