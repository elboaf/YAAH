"""Turn-recovery regression tests: truncated tool args, mid-stream drops,
retry scoping, and JSON-safe tool-result clipping.

These simulate model responses by stubbing model_client.chat, so no real
provider is contacted.
"""
import json

import pytest

from backend.agent import loop as agent_loop
from backend.db.database import create_conversation


def sse_stream(*events):
    async def it():
        for e in events:
            yield e

    return it()


class FakeChat:
    """Callable standing in for model_client.chat; yields scripted streams."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list] = []

    async def __call__(self, messages, tools=None, stream=False):
        self.calls.append([dict(m) for m in messages])
        return sse_stream(*self.responses.pop(0))


async def run_turn(monkeypatch, responses, workspace="."):
    fake = FakeChat(responses)
    monkeypatch.setattr(agent_loop.model_client, "chat", fake)
    cid = await create_conversation("t", workspace)
    out = []
    async for line in agent_loop.run_agent(cid, "hello", workspace):
        out.append(json.loads(line))
    return fake, out


def tool_call(args: str, name: str = "read_file", id: str = "call1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": args}}


@pytest.mark.asyncio
async def test_truncated_args_get_truncation_error_not_invalid_json(monkeypatch):
    """finish_reason=length + cut-off JSON args must tell the model the
    output was clipped — 'Invalid JSON arguments' made it retry the same
    call and truncate again (the reread-same-file loop)."""
    _, out = await run_turn(
        monkeypatch,
        [
            [
                {"type": "tool_calls", "tool_calls": [tool_call('{"path": "src/ma')]},
                {"type": "finish", "reason": "length"},
            ],
            # After the corrective result the model gives up gracefully.
            [{"type": "content", "text": "ok"}, {"type": "finish", "reason": "stop"}],
        ],
    )
    results = [e for e in out if e["type"] == "tool_result"]
    assert len(results) == 1
    assert "max output tokens" in results[0]["result"]["error"]
    assert "Invalid JSON" not in results[0]["result"]["error"]
    assert out[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_unparseable_args_without_truncation_keep_old_message(monkeypatch):
    _, out = await run_turn(
        monkeypatch,
        [
            [
                {"type": "tool_calls", "tool_calls": [tool_call("{oops")]},
                {"type": "finish", "reason": "stop"},
            ],
            [{"type": "content", "text": "ok"}, {"type": "finish", "reason": "stop"}],
        ],
    )
    result = next(e for e in out if e["type"] == "tool_result")["result"]
    assert result["error"].startswith("Invalid JSON arguments")


@pytest.mark.asyncio
async def test_length_finish_on_text_answer_appends_note(monkeypatch):
    _, out = await run_turn(
        monkeypatch,
        [[{"type": "content", "text": "half an ans"}, {"type": "finish", "reason": "length"}]],
    )
    assert out[-1]["type"] == "done"
    notes = [e for e in out if e["type"] == "text" and "truncated" in e.get("text", "")]
    assert notes


@pytest.mark.asyncio
async def test_midstream_model_error_retries_once(monkeypatch):
    class ExplodingThenFine(FakeChat):
        async def __call__(self, messages, tools=None, stream=False):
            self.calls.append(messages)
            if len(self.calls) == 1:
                async def boom():
                    raise agent_loop.model_client.ModelError("503 bad gateway")
                    yield  # pragma: no cover
                return boom()
            return sse_stream(
                *([{"type": "content", "text": "fine"}, {"type": "finish", "reason": "stop"}])
            )

    fake = ExplodingThenFine([])
    monkeypatch.setattr(agent_loop.model_client, "chat", fake)
    cid = await create_conversation("t", ".")
    out = [json.loads(l) async for l in agent_loop.run_agent(cid, "hello", ".")]
    assert len(fake.calls) == 2, "retry must happen after a mid-stream failure"
    # the corrective system message was sent on the retry
    assert fake.calls[1][-1]["role"] == "system"
    assert out[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_two_failures_surface_error(monkeypatch):
    class AlwaysBoom(FakeChat):
        async def __call__(self, messages, tools=None, stream=False):
            self.calls.append(messages)
            async def boom():
                raise agent_loop.model_client.ModelError("503 again")
                yield  # pragma: no cover
            return boom()

    fake = AlwaysBoom([])
    monkeypatch.setattr(agent_loop.model_client, "chat", fake)
    cid = await create_conversation("t", ".")
    out = [json.loads(l) async for l in agent_loop.run_agent(cid, "hello", ".")]
    assert len(fake.calls) == 2, "exactly one retry"
    assert out[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_empty_stream_with_no_finish_is_retryable(monkeypatch):
    """A stream that just stops (connection drop) must not be treated as a
    final answer; the retry then completes the turn."""
    class DropThenFine(FakeChat):
        async def __call__(self, messages, tools=None, stream=False):
            self.calls.append(messages)
            if len(self.calls) == 1:
                async def dropped():
                    # partial content, then the connection dies: replicates
                    # _stream_response raising on a missing [DONE]/finish
                    yield {"type": "content", "text": "par"}
                    raise agent_loop.model_client.ModelError(
                        "model stream ended without a finish reason"
                    )
                return dropped()
            return sse_stream(
                {"type": "content", "text": "fine"},
                {"type": "finish", "reason": "stop"},
            )

    fake = DropThenFine([])
    monkeypatch.setattr(agent_loop.model_client, "chat", fake)
    cid = await create_conversation("t", ".")
    out = [json.loads(l) async for l in agent_loop.run_agent(cid, "hello", ".")]
    assert len(fake.calls) == 2
    assert out[-1]["type"] == "done"


def test_clip_result_str_keeps_json_valid():
    big = {"path": "f.py", "content": "x" * 50_000}
    s = agent_loop._clip_result_str(big, limit=5_000)
    assert len(s) <= 5_200
    parsed = json.loads(s)  # must not raise
    assert "truncated" in parsed["note"]
    assert parsed["path"] == "f.py"


def test_clip_result_str_passthrough_small():
    s = agent_loop._clip_result_str({"ok": True})
    assert json.loads(s) == {"ok": True}
