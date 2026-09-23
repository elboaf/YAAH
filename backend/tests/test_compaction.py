"""History compaction (adr/0004): trigger, boundary math, persistence,
loop wiring, and the synthetic-summary path. The real summarizer call is
monkeypatched everywhere except the fallback test — its contract
(model returns garbage -> clipped excerpt survives) is covered there."""

import json

import pytest

from backend.agent import compaction as comp
from backend.db import database as db


def _msg(role, text, tool_calls=None, tool_call_id=None):
    m = {"role": role, "content": text}
    if tool_calls:
        m["tool_calls"] = tool_calls
    if tool_call_id:
        m["tool_call_id"] = tool_call_id
    return m


def _big(n_chars, seed="x"):
    return (seed * 200)[:n_chars]


def _make_history(seed="x"):
    """A long alternating user/assistant history, large enough that the
    chars/4 split math has real room."""
    out = []
    for i in range(12):
        out.append(_msg("user", _big(900, f"u{i}-{seed}")))
        out.append(_msg("assistant", _big(900, f"a{i}-{seed}")))
    return out


def _tc(name="bash", args='{"command": "echo hi"}', cid="c1"):
    return [
        {
            "id": cid,
            "type": "function",
            "function": {"name": name, "arguments": args},
        }
    ]


# The REAL summarizer, captured before the autouse fake below replaces the
# module attribute — the summarizer-contract tests restore it explicitly.
REAL_SUMMARIZE = comp.summarize_messages


def _patch_cfg(monkeypatch, **over):
    cfg = {
        "enabled": True,
        "trigger_fraction": 0.7,
        "keep_fraction": 0.4,
        "keep_recent_messages": 6,
        "default_window": 32_000,
        "model": "",
        **over,
    }
    monkeypatch.setattr(comp, "_compaction_cfg", lambda: cfg)
    return cfg


@pytest.fixture(autouse=True)
def _fake_summarizer(monkeypatch):
    """Deterministic summarizer: marker + folded count."""
    async def fake_summarize(cut_messages, model_override=""):
        return f"SYNTHETIC SUMMARY of {len(cut_messages)} messages"

    monkeypatch.setattr(comp, "summarize_messages", fake_summarize)


# ------------------------------------------------------------- units


def test_defaults_on():
    assert comp._DEFAULTS["enabled"] is True


@pytest.mark.asyncio
async def test_should_compact_threshold(monkeypatch):
    _patch_cfg(monkeypatch)
    # 0.7 trigger: strictly above the fraction fires.
    assert comp.should_compact(69_999, 100_000) is False
    assert comp.should_compact(70_000, 100_000) is False
    assert comp.should_compact(70_001, 100_000) is True
    assert comp.should_compact(None, 100_000) is False  # nothing measured yet
    assert comp.should_compact(70_001, 0) is False  # unknown window
    _patch_cfg(monkeypatch, enabled=False)
    assert comp.should_compact(999_999, 100_000) is False


def test_find_cut_index_respects_user_boundary():
    msgs = [
        _msg("user", _big(900)),
        _msg("assistant", _big(900), tool_calls=_tc(cid="c1")),
        _msg("tool", _big(600), tool_call_id="c1"),
        _msg("assistant", _big(900)),
        _msg("user", _big(900)),
        _msg("assistant", _big(900)),
    ]
    # Keep budget so small the walk eats everything: the boundary slides
    # back to the LAST user message (index 4) and the tool pair stays
    # intact inside the verbatim tail.
    cut = comp.find_cut_index(msgs, keep_tokens=1, min_tail=1)
    assert cut == 4
    assert msgs[cut]["role"] == "user"


def test_find_cut_index_min_tail_floor():
    msgs = _make_history()
    for k in (1, 4, 10):
        cut = comp.find_cut_index(msgs, keep_tokens=1, min_tail=k)
        if cut:
            assert len(msgs) - cut >= k
    # min_tail >= len: never cut.
    assert comp.find_cut_index(msgs, keep_tokens=1, min_tail=len(msgs)) == 0


def test_find_cut_index_nothing_to_do():
    msgs = _make_history()
    # Whole history fits the keep budget: no candidates.
    assert comp.find_cut_index(msgs, keep_tokens=10**9, min_tail=6) == 0
    # Shorter than the tail floor: never cut.
    assert comp.find_cut_index(msgs[:4], keep_tokens=1, min_tail=6) == 0


def test_find_cut_index_no_user_boundary():
    msgs = [_msg("assistant", _big(900)), _msg("assistant", _big(900))]
    assert comp.find_cut_index(msgs, keep_tokens=1, min_tail=1) == 0


def test_find_cut_index_normal_cut():
    msgs = _make_history()
    # Keep ~1/3 of the history's tokens: boundary lands on a user msg
    # and roughly a third of the messages stay verbatim.
    total_tokens = comp.estimate_tokens(msgs)
    cut = comp.find_cut_index(msgs, keep_tokens=total_tokens // 3, min_tail=2)
    assert 0 < cut < len(msgs)
    assert msgs[cut]["role"] == "user"


def test_extract_summary_fences_and_garbage():
    assert comp._extract_summary('{"summary": "abc"}') == "abc"
    assert comp._extract_summary('```json\n{"summary": "def"}\n```') == "def"
    assert comp._extract_summary("no json here") == ""
    assert comp._extract_summary('{"other": 1}') == ""
    assert comp._extract_summary("") == ""


# ------------------------------------------------------- summarizer


@pytest.mark.asyncio
async def test_summarize_fallback_on_garbage(monkeypatch):
    """Model ignores the JSON contract -> a clipped verbatim excerpt is
    stored instead of the reply, so the prefix is never lost."""
    calls = []

    async def fake_chat(messages, tools=None, stream=False, model=""):
        calls.append(messages)
        return {
            "choices": [
                {"message": {"content": "I am a chatty model, no JSON today."}}
            ]
        }

    monkeypatch.setattr(comp.model_client, "chat", fake_chat)
    monkeypatch.setattr(comp, "summarize_messages", REAL_SUMMARIZE)
    out = await comp.summarize_messages([_msg("user", "hello world" * 10)])
    assert "hello world" in out  # excerpt text, not the garbage reply
    assert len(calls) == 1
    sent = calls[0]
    assert sent[0]["role"] == "system" and "compress" in sent[0]["content"]
    assert sent[1]["role"] == "user"


@pytest.mark.asyncio
async def test_summarize_happy_path(monkeypatch):
    async def fake_chat(messages, tools=None, stream=False, model=""):
        return {"choices": [{"message": {"content": '{"summary": "kept"}'}}]}

    monkeypatch.setattr(comp.model_client, "chat", fake_chat)
    monkeypatch.setattr(comp, "summarize_messages", REAL_SUMMARIZE)
    assert await comp.summarize_messages([_msg("user", "x")]) == "kept"


@pytest.mark.asyncio
async def test_summarize_errors_propagate():
    """A provider failure inside the summarizer call propagates: the loop's
    soft handler turns it into a `compaction_failed` event (covered in the
    loop tests), never a lost history."""
    from backend.agent.model_client import ModelError

    async def failing_chat(messages, tools=None, stream=False, model=""):
        raise ModelError("no provider configured")

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(comp.model_client, "chat", failing_chat)
    monkeypatch.setattr(comp, "summarize_messages", REAL_SUMMARIZE)
    try:
        with _pytest.raises(ModelError):
            await comp.summarize_messages([_msg("user", "x")])
    finally:
        monkeypatch.undo()


# ------------------------------------------------- end-to-end + loop


@pytest.mark.asyncio
async def test_compact_persists_and_remeasures(monkeypatch):
    """The whole pass: rows folded into one system row, context_tokens
    nulled, conversation still loadable."""
    cid = await db.create_conversation("compact-me")
    for m in _make_history():
        await db.add_message(cid, m["role"], m["content"])
    # Real load_history also feeds from get_messages; the pass reads raw rows.
    rows = await db.get_messages(cid)
    assert len(rows) == 24

    await db.compact_conversation(cid, "the summary", cut_messages=16)
    rows_after = await db.get_messages(cid)
    roles = [r["role"] for r in rows_after]
    assert roles == (["system"] + ["user", "assistant"] * 4)
    assert rows_after[0]["content"] == "the summary"
    conv = await db.get_conversation(cid)
    assert conv["context_tokens"] is None

    # Stale caller (rows deleted concurrently): rollback, 0 removed.
    removed = await db.compact_conversation(cid, "x", cut_messages=999)
    assert removed == 0
    assert len(await db.get_messages(cid)) == 9


@pytest.mark.asyncio
async def test_compact_history_for_context_end_to_end(monkeypatch):
    """Trigger fires -> prefix folded -> event payload -> next pass no-ops
    (context_tokens is NULL until a fresh measurement lands)."""
    _patch_cfg(monkeypatch, default_window=8_000, keep_recent_messages=4)

    async def fake_resolve_window(model, cfg=None):
        return 8_000

    monkeypatch.setattr(comp, "resolve_window", fake_resolve_window)

    cid = await db.create_conversation("e2e")
    for m in _make_history():
        await db.add_message(cid, m["role"], m["content"])
    # The provider's last measurement: ~14k chars of history + overhead
    # crosses 0.7 * 8_000 = 5_600 tokens.
    await db.set_conversation_usage(cid, 6_000, "test-model")

    result = await comp.compact_history_for_context(cid, model_id="test-model")
    assert result and result["type"] == "compacted"
    assert result["summarized_messages"] > 0
    assert "SYNTHETIC SUMMARY" in result["summary"]
    assert result["context_window"] == 8_000

    rows = await db.get_messages(cid)
    assert rows[0]["role"] == "system"
    assert len(rows) == 1 + (24 - result["summarized_messages"])

    # Second pass: no measurement since the compaction -> no-op.
    again = await comp.compact_history_for_context(cid, model_id="test-model")
    assert again is None


@pytest.mark.asyncio
async def test_loop_runs_compaction_before_history(monkeypatch, tmp_path):
    """Full run_agent wiring: over-budget conversation gets exactly one
    `compacted` event ahead of every other event, and the first model
    call is rebuilt on the compacted history."""
    from backend.agent import loop

    _patch_cfg(monkeypatch, default_window=8_000, keep_recent_messages=4)

    async def fake_resolve_window(model, cfg=None):
        return 8_000

    monkeypatch.setattr(comp, "resolve_window", fake_resolve_window)

    cid = await db.create_conversation("loop-wires-it")
    for m in _make_history():
        await db.add_message(cid, m["role"], m["content"])
    await db.set_conversation_usage(cid, 6_000, "test-model")

    seen_sizes = []

    class FakeStream:
        def __init__(self, events):
            self._events = list(events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

    async def fake_chat(messages, tools=None, stream=True, model=""):
        seen_sizes.append(len(messages))
        return FakeStream([{"type": "content", "text": "ok"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    async def collect():
        out = []
        async for line in loop.run_agent(cid, "next turn", str(tmp_path)):
            out.append(json.loads(line))
        return out

    events = await collect()
    types = [e["type"] for e in events]
    assert types[0] == "compacted"
    assert types.count("compacted") == 1
    # Every other event trails the compaction notice.
    assert all(t != "compacted" for t in types[1:])
    assert "done" in types
    # First model call saw: system + summary-less compacted tail + live
    # user message — i.e. the compacted history, not all 24 old rows.
    assert seen_sizes[0] < 24
    rows = await db.get_messages(cid)
    assert rows[0]["role"] == "system"


@pytest.mark.asyncio
async def test_loop_compaction_failure_is_soft(monkeypatch, tmp_path):
    """A broken compaction pass must not fail the turn: a
    compaction_failed event rides along, the turn completes."""
    from backend.agent import loop

    _patch_cfg(monkeypatch)

    async def boom(cid, model_id=None):
        raise RuntimeError("summarizer exploded")

    monkeypatch.setattr(comp, "compact_history_for_context", boom)

    class FakeStream:
        def __init__(self, events):
            self._events = list(events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

    async def fake_chat(messages, tools=None, stream=True, model=""):
        return FakeStream([{"type": "content", "text": "fine"}, {"type": "finish"}])

    monkeypatch.setattr(loop.model_client, "chat", fake_chat)

    cid = await db.create_conversation("soft-fail")
    out = []
    async for line in loop.run_agent(cid, "hi", str(tmp_path)):
        out.append(json.loads(line))
    types = [e["type"] for e in out]
    assert "compaction_failed" in types
    assert types[-1] == "done"
    assert out[-2]["type"] != "error"


@pytest.mark.asyncio
async def test_disabled_via_config(monkeypatch):
    _patch_cfg(monkeypatch, enabled=False)
    cid = await db.create_conversation("disabled")
    await db.set_conversation_usage(cid, 10_000_000, "m")
    assert await comp.compact_history_for_context(cid) is None
