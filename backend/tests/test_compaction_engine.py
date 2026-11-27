"""Compaction engine (issue #852 follow-up, #883).

These tests exercise the side-effecting half of the fold: how the engine
groups the replayed message rows into rounds, where the cut lands, what the
summary call prompt carries, and how load_history replays a folded
conversation. The tests assert the external behaviour at the assembled-prompt
seam, not the internals. The DB is the conftest throwaway (isolated per run);
each test uses its own autoincremented conversation id, so there is no
cross-test contamination.
"""
import asyncio

import pytest

from backend.agent import compaction as _policy
from backend.agent import compaction_engine as _engine
from backend.agent import loop as loop_mod
from backend.db.database import (
    create_conversation,
    get_messages,
    get_conversation,
    set_conversation_usage,
    update_conversation,
)


def _row(mid, role, content):
    return {"id": mid, "conversation_id": 1, "role": role, "content": content}


async def _ensure_watermark_column():
    """Guard against a stale read: under CI's -n 2 (shared agent.db) a second
    process's first connection can SELECT * FROM conversations before the
    compaction_watermark column migration has landed. Wait for it so the test
    never reads a row snapshot taken before the column existed.
    """
    from backend.db import database as dbmod

    for _ in range(20):
        cols = await dbmod._conversation_columns()
        if "compaction_watermark" in cols:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("compaction_watermark column never appeared")


# ------------------------------------------------------------------ rounds


class TestRoundGrouping:
    def test_groups_assistant_and_following_tool(self):
        rows = [
            _row(1, "user", "hi"),
            _row(2, "assistant", "let me think"),
            _row(3, "tool", "result"),
            _row(4, "assistant", "done"),
        ]
        rounds = _engine.group_into_rounds(rows)
        # Leading rows + the first assistant turn form one round; the second
        # assistant turn starts the next. The engine keeps consecutive rows
        # together (the leading user row seeds the first round).
        assert len(rounds) == 2
        assert [r["id"] for r in rounds[0]] == [1, 2, 3]
        assert [r["id"] for r in rounds[1]] == [4]

    def test_leading_rows_seed_first_round(self):
        rows = [_row(1, "user", "first"), _row(2, "user", "again")]
        rounds = _engine.group_into_rounds(rows)
        assert len(rounds) == 1
        assert [r["id"] for r in rounds[0]] == [1, 2]

    def test_empty_rows(self):
        assert _engine.group_into_rounds([]) == []


class TestSelectFold:
    def test_folds_older_rounds_keeps_recent(self):
        # Each round carries ~200 tokens of content. Keep budget of 10 tokens
        # means only the most recent round (2 tokens) fits; the rest fold.
        rows = [
            _row(1, "user", "r1 " * 100),
            _row(2, "assistant", "a1 " * 100),
            _row(3, "tool", "t1 " * 100),
            _row(4, "assistant", "a2 " * 100),
            _row(5, "assistant", "a3"),  # ~2 tokens, fits the keep budget
        ]
        rounds = _engine.group_into_rounds(rows)
        kept, watermark = _engine.select_fold_rows(rows, rounds, keep_budget=10)
        # watermark is the newest folded row id; kept keeps only the newer
        # rows (those after the watermark).
        assert watermark is not None
        assert watermark < max(r["id"] for r in rows)
        assert min(r["id"] for r in kept) > watermark

    def test_no_fold_when_budget_covers_all(self):
        rows = [_row(1, "user", "u"), _row(2, "assistant", "a")]
        rounds = _engine.group_into_rounds(rows)
        kept, watermark = _engine.select_fold_rows(rows, rounds, keep_budget=100000)
        assert watermark is None
        assert len(kept) == len(rows)


# ---------------------------------------------------------------- prompt


class TestSummaryPrompt:
    def test_tool_free_plain_text(self):
        rows = [_row(1, "user", "u"), _row(2, "assistant", "a")]
        prompt = _engine.build_summary_prompt(rows, [])
        # System carries the contract; the user message carries the fold.
        assert len(prompt) == 2
        assert prompt[0]["role"] == "system"
        assert "<analysis>" in prompt[0]["content"]
        assert "<summary>" in prompt[0]["content"]
        assert prompt[1]["role"] == "user"
        # No tools field at all -> tool-free.
        assert "tools" not in prompt[0]
        assert "tools" not in prompt[1]

    def test_security_instructions_copied_verbatim(self):
        rows = [_row(1, "user", "never edit file X")]
        prompt = _engine.build_summary_prompt(rows, ["never edit file X"])
        assert "never edit file X" in prompt[1]["content"]


# ------------------------------------------------------------------- replay


@pytest.mark.asyncio
async def test_load_history_injects_summary_and_skips_watermark():
    cid = await create_conversation("folded")
    # Rows 1-3 are the folded (old) part; rows 4-5 are the recent part.
    for role, content in [
        ("user", "old turn one"),
        ("assistant", "old reply"),
        ("tool", "old tool result"),
        ("user", "new turn"),
        ("assistant", "new reply"),
    ]:
        await loop_mod.add_message(cid, role, content)
    # Set the watermark against the actual row ids (they start at whatever the
    # throwaway DB hands out, not necessarily 1). The first three rows are the
    # folded part; the watermark is the newest of them.
    # Guard against a stale read: under CI's -n 2 (shared agent.db) a second
    # process's migration pass can skip the compaction_watermark ALTER when its
    # schema check runs before the first process has committed it, leaving this
    # connection's reads of the row NULL. Wait for the column to exist first.
    await _ensure_watermark_column()
    persisted = await get_messages(cid)
    old_ids = [r["id"] for r in persisted[:3]]
    watermark = max(old_ids)
    # Apply the fold on the conversation row (immutable).
    await update_conversation(
        cid,
        touch_updated_at=False,
        compaction_summary="SUMMARY OF OLD TURNS",
        compaction_watermark=int(watermark),
    )
    replayed = await loop_mod.load_history(cid)
    # The folded rows are gone; only the recent rows survive in the replay.
    contents = [m["content"] for m in replayed]
    assert "old reply" not in contents
    assert "new reply" in contents
    # The fold summary is injected as the oldest message.
    assert replayed[0]["content"].strip().startswith("<compacted_history>")
    assert "SUMMARY OF OLD TURNS" in replayed[0]["content"]
    # The original message rows are untouched (immutability).
    persisted = await get_messages(cid)
    assert "old reply" in [m["content"] for m in persisted]


@pytest.mark.asyncio
async def test_load_history_without_fold_is_unchanged():
    cid = await create_conversation("not-folded")
    await loop_mod.add_message(cid, "user", "hello")
    await loop_mod.add_message(cid, "assistant", "hi there")
    replayed = await loop_mod.load_history(cid)
    assert [m["content"] for m in replayed] == ["hello", "hi there"]


# ------------------------------------------------------------------- engine


@pytest.mark.asyncio
async def test_run_compaction_nothing_measured_returns_none():
    cid = await create_conversation("unmeasured")
    # No context_tokens set -> nothing to fold.
    summary, watermark = await _engine.run_compaction(
        cid,
        None,
        {},
        "",
        rows=[],
        policy=_policy.CompactionPolicy.defaults(),
        circuit_breaker=_policy.CircuitBreaker(),
        last_compacted_tokens=None,
        cancel_ev=None,
        emit_status=None,
    )
    assert summary is None
    assert watermark is None


@pytest.mark.asyncio
async def test_run_compaction_over_trigger_folds():
    cid = await create_conversation("fold-me")
    # Seed a long history. The recent rounds carry enough content that the
    # keep budget (see below) cannot cover them, forcing a fold of the older
    # rounds.
    long_content = "x " * 400  # ~200 tokens per round of content
    for mid, role, content in [
        (1, "user", "task one"),
        (2, "assistant", long_content),
        (3, "tool", "tool result one"),
        (4, "assistant", long_content),
        (5, "user", "task two"),
        (6, "assistant", long_content),
    ]:
        await loop_mod.add_message(cid, role, content)
    # Measured prompt size above the trigger for the small window.
    await set_conversation_usage(cid, 120_000, "m")
    # window 10000 -> eff 5392 -> trigger 1296 -> keep ~777. The last two
    # rounds each carry ~200 tokens, cumulative ~1000 > 777, so the older of
    # them folds.
    cfg = {"model_context": {"m": {"context_window": 10000}}}

    class _FakeMC:
        async def chat(self, messages, **kwargs):
            return {
                "choices": [{"message": {"content": "<summary>FOLDED</summary>"}}]
            }

    emitted = []
    summary, watermark = await _engine.run_compaction(
        cid,
        _FakeMC(),
        cfg,
        "m",
        rows=await get_messages(cid),
        policy=_policy.CompactionPolicy.defaults(),
        circuit_breaker=_policy.CircuitBreaker(),
        last_compacted_tokens=None,
        cancel_ev=None,
        emit_status=lambda e: emitted.append(e),
    )
    assert summary == "<summary>FOLDED</summary>"
    assert watermark is not None
    # The conversation row now carries the fold.
    conv = await get_conversation(cid)
    assert conv["compaction_summary"] == "<summary>FOLDED</summary>"
    assert conv["compaction_watermark"] == watermark
    # context_tokens was reset so the next call re-measures.
    assert conv["context_tokens"] == 0
    # A status event was emitted.
    assert any(e.get("type") == "compaction" for e in emitted)
    # Immutability: the original rows are still in the transcript.
    persisted = await get_messages(cid)
    assert len(persisted) == 6


@pytest.mark.asyncio
async def test_run_compaction_monotonic_gate():
    cid = await create_conversation("gated")
    await set_conversation_usage(cid, 100_000, "m")
    cfg = {"model_context": {"m": {"context_window": 1000}}}

    calls = {"n": 0}

    class _FakeMC:
        async def chat(self, messages, **kwargs):
            calls["n"] += 1
            return {
                "choices": [{"message": {"content": "<summary>FOLDED</summary>"}}]
            }

    summary, watermark = await _engine.run_compaction(
        cid,
        _FakeMC(),
        cfg,
        "m",
        rows=[],
        policy=_policy.CompactionPolicy.defaults(),
        circuit_breaker=_policy.CircuitBreaker(),
        last_compacted_tokens=100_000,  # same as measured -> gate blocks
        cancel_ev=None,
        emit_status=None,
    )
    assert summary is None
    assert watermark is None
    assert calls["n"] == 0
