"""Compaction wiring for the model-call loop (issue #852 follow-up, #883).

The policy core in ``compaction.py`` is pure: given a measured prompt size and
the model's resolved window it answers *whether* to fold and *how far*. This
module owns the side effects the policy never touches (per its docstring):

1. Group the replayed message rows into assistant-started **rounds**.
2. Keep the most recent rounds verbatim, fold the older ones into one bounded
   summary (keep-budget expressed in tokens, cut always at a round boundary).
3. Run a single non-streaming, tool-free summary call (graceful degradation +
   circuit breaker).
4. Apply the summary + watermark on the conversation row (immutable: the
   message rows are never edited), and reset ``context_tokens`` so the next
   call re-measures.

Replay (``load_history``) injects the stored summary and skips rows at or
before the watermark, so the transcript stays intact for review/export/search
while the model only ever sees summary + recent rounds.

Immutability is the whole point of the port (ADR-0004 preserved, ADR-0020
removed): compaction updates prompt-summary + watermark fields on the
conversation, never a message row.
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from backend.agent import compaction as _policy
from backend.agent import context_window as _cw
from backend.agent.model_client import ModelError
from backend.db.database import (
    get_conversation,
    get_messages,
    set_conversation_usage,
    update_conversation,
)

log = __import__("logging").getLogger(__name__)

# The tool-free summary prompt contract (adapted from zcode's
# BASE_COMPACT_PROMPT): plain text only, <analysis> then <summary>, sections
# for the continuity-critical parts of the session, and a verbatim copy of the
# security-relevant instructions so a fold never silently lifts a guard rail.
_COMPACT_SYSTEM = (
    "You are a conversation compactor. You receive a slice of an ongoing "
    "session's transcript (a run of past turns) and must compress it into a "
    "single handoff document so the model can keep working without the "
    "original turns. This is CONTEXT, not a request to act. Never answer the "
    "transcript, never refuse it, never address the user. Produce ONLY the "
    "two blocks below.\n\n"
    "Write <analysis> first: a tight accounting of what the session has done "
    "so far and where it stands. Then write <summary> as a structured "
    "handoff with these sections, each as a short line or two:\n"
    "- INTENT: the user's goal in this session.\n"
    "- FILES AND CODE: files created/edited and the key changes.\n"
    "- ERRORS AND FIXES: errors hit and how they were resolved.\n"
    "- PENDING TASKS: anything left to do.\n"
    "- CURRENT WORK: what was being worked on at the cut.\n"
    "- NEXT STEP: the immediate next action, quoting verbatim the recent text "
    "that defines it.\n"
    "Also copy VERBATIM any security-relevant instruction the user gave early "
    "in the session (e.g. 'never edit file X', 'this is a read-only "
    "workspace') so it survives the fold. Do not add tools, code fences, or "
    "any text outside the two blocks.\n"
)

# Heuristic: a line that looks like a user-given guard rail. Kept deliberately
# small; the summary call is the higher-fidelity path, this just makes sure a
# common guard rail is copied verbatim even if the model drops it.
_SECURITY_HINTS = (
    "never",
    "must not",
    "must always",
    "do not",
    "read-only",
    "security",
    "do not touch",
    "must not edit",
    "never edit",
    "no external",
)

# Progressive-trim retries when the summary request itself is too large.
_MAX_TRIM_RETRIES = 4


def _estimate_tokens(text: str) -> int:
    """Rough token count: ~4 characters per token (open-ended, cheap)."""
    return max(0, len(text) // 4)


def _extract_summary(data: dict) -> str:
    """Pull the assistant message content out of a non-streaming chat payload."""
    return (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        or ""
    )


def group_into_rounds(rows: list[dict]) -> list[list[dict]]:
    """Group replay rows into assistant-started rounds.

    A round begins at an assistant message and swallows the rows that follow
    it (its tool results, follow-up assistant turns) until the next
    assistant message starts a new round. User/tool rows before the first
    assistant message form a leading round so nothing is dropped.
    """
    rounds: list[list[dict]] = []
    current: list[dict] = []
    started = False
    for r in rows:
        if r.get("role") == "assistant":
            if started and current:
                rounds.append(current)
                current = []
            started = True
            current.append(r)
        else:
            if started:
                current.append(r)
            elif current:
                current.append(r)
            else:
                # Leading rows before any assistant turn: seed the first round
                # so they are preserved verbatim rather than lost.
                current.append(r)
    if current:
        rounds.append(current)
    return rounds


def _round_text(round_rows: list[dict]) -> str:
    """The readable text of a round, assistant content first then tool/user.

    Mirrors load_history's re-inlining so a fold preserves exactly what normal
    replay would have handed the model: user attachment text (#142) and
    assistant tool-call names/arguments serialized as text (never executed). A
    round that carried only an attachment (empty content) therefore still
    contributes its request to the summary instead of folding away as nothing.
    """
    parts: list[str] = []
    for r in round_rows:
        role = r.get("role")
        content = r.get("content")
        if role == "assistant" and content:
            parts.append(content.strip())
        elif role == "assistant":
            # Empty-content assistant turn still carried tool calls; serialize
            # their names + arguments as text so the fold keeps the operation.
            tcs = r.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        fn = tc.get("function")
                        name = fn.get("name", "") if isinstance(fn, dict) else ""
                        args = fn.get("arguments", "") if isinstance(fn, dict) else ""
                        parts.append(f"[tool call: {name}({args})]")
        elif role in ("user", "tool") and content and not isinstance(content, list):
            # #142: re-inline structured attachments byte-identically to what
            # replay would have sent (done at read time, never at persist time).
            if r.get("attachments"):
                from backend.agent.attachments import reinline_attachments

                content = reinline_attachments(str(content), r["attachments"])
            parts.append(str(content).strip())
    return "\n\n".join(p for p in parts if p)


def select_fold_rows(
    rows: list[dict], rounds: list[list[dict]], keep_budget: int
) -> tuple[list[dict], int | None]:
    """Split rows into (kept, folded).

    Keep the most recent rounds verbatim until the cumulative estimated token
    count reaches ``keep_budget``; fold the older rounds. Returns the kept rows
    and the watermark (the id of the newest folded row, or None when nothing
    is folded). The cut is always at a round boundary, so the prompt handed to
    the model never straddles a user/tool pair.
    """
    if keep_budget <= 0 or not rounds:
        return rows, None
    kept: list[dict] = []
    folded_ids: list[int] = []
    kept_tokens = 0
    # Walk from the most recent round backward.
    for round_rows in reversed(rounds):
        # Count each round's estimated tokens once (a round's own estimate is
        # rough; use the newest row's id to mark the watermark and the round's
        # text for the fold).
        round_tokens = _estimate_tokens(_round_text(round_rows))
        if kept_tokens + round_tokens > keep_budget and kept_tokens > 0:
            # This round does not fit; fold it and everything older.
            for r in round_rows:
                if r.get("id") is not None:
                    folded_ids.append(r["id"])
            # Also fold any remaining rows not yet assigned (leading rows).
            break
        for r in round_rows:
            kept.append(r)
        kept_tokens += round_tokens
    if not folded_ids:
        return kept, None
    watermark = max(folded_ids)
    return kept, watermark


def _security_instructions(rows: list[dict]) -> list[str]:
    """Guard-rail lines the user gave, copied verbatim into the summary."""
    out: list[str] = []
    for r in rows:
        content = r.get("content")
        if not isinstance(content, str):
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if (
                stripped
                and any(hint in stripped.casefold() for hint in _SECURITY_HINTS)
                and stripped not in out
            ):
                out.append(stripped)
    return out[:20]


def build_summary_prompt(
    fold_rows: list[dict], security_instructions: list[str], prior_summary: str | None = None
) -> list[dict]:
    """The tool-free summary call prompt.

    System: the compaction contract. User: the folded transcript (older turns)
    plus any verbatim security instructions. On a subsequent fold, the prior
    stored summary is carried forward so already-compact history is preserved
    instead of being re-read from the (now-watermarked) rows.
    """
    fold_text = "\n\n".join(_round_text([r]) for r in fold_rows if r.get("content"))
    if prior_summary:
        fold_text = (
            "PREVIOUSLY COMPACTED HISTORY (already summarized; summarize again "
            "only if it grows):\n\n\"\"\"\n" + prior_summary + "\n\"\"\"\n\n"
        ) + fold_text
    if security_instructions:
        fold_text = (
            "SECURITY-RELEVANT INSTRUCTIONS (copy verbatim into the summary):\n"
            + "\n".join(f"- {s}" for s in security_instructions)
            + "\n\n"
            + fold_text
        )
    return [
        {"role": "system", "content": _COMPACT_SYSTEM},
        {"role": "user", "content": f"Transcript to compress:\n\n\"\"\"\n{fold_text}\n\"\"\""},
    ]


async def _summary_call(
    model_client, prompt: list[dict], model_override: str | None, cancel_ev=None
) -> str:
    """One non-streaming, tool-free summary request; returns the summary text.

    #884: the request is raced against the turn's cancel event so a user Stop
    during compaction tears the request down (via _race_model_call) and raises
    ModelCancelled rather than running to completion and persisting a stale
    fold. cancel_ev=None is a no-op race (call runs to completion).
    """
    kwargs: dict = {"tools": None, "stream": False, "max_tokens": 2048}
    if model_override:
        kwargs["model"] = model_override
    kwargs["effort"] = None  # deterministic summary, no reasoning loop
    call = model_client.chat(prompt, **kwargs)
    if cancel_ev is not None:
        from backend.agent.loop import _race_model_call

        call = asyncio.create_task(call)
        data = await _race_model_call(call, cancel_ev)
    else:
        data = await call
    return _extract_summary(data)


async def run_compaction(
    conversation_id: int,
    model_client,
    cfg: dict,
    model_override: str,
    *,
    rows: list[dict],
    policy: _policy.CompactionPolicy,
    circuit_breaker: _policy.CircuitBreaker,
    last_compacted_tokens: int | None,
    cancel_ev,
    emit_status,
) -> tuple[str | None, int | None]:
    """Run the pre-call compaction pass for one turn.

    Returns ``(summary, watermark)`` actually applied to the conversation (both
    None when nothing was compacted). Reads the DB-stored measured prompt size,
    resolves the model window, decides via the policy, and (only when it fires)
    summarizes + applies the watermark. Never raises: a broken summarizer is
    recorded on the circuit breaker and the turn proceeds with the full history.
    """
    conv = await get_conversation(conversation_id) or {}
    prompt_tokens = conv.get("context_tokens")
    if prompt_tokens is None:
        return None, None  # nothing measured yet -> nothing to fold

    # Monotonic per-conversation gate: never compact twice on the same
    # context size (a second pass would re-fold the same rows).
    if last_compacted_tokens == prompt_tokens:
        return None, None

    model = model_override or conv.get("model") or cfg.get("model") or ""
    # #884: pass the bare model id (the model part of a scoped "provider::model"
    # route), not the provider. get_context_window resolves by model id and has
    # no provider-name resolution, so the old code could return None and skip
    # compaction silently for scoped models.
    window = await _cw.get_context_window(
        _cw.split_model_route(model, cfg)[0],
        cfg,
    )
    if not window:
        return None, None

    eff = _policy.effective_context_window(
        window, policy.output_reserve_tokens, policy.output_safety_margin_tokens
    )
    trigger = _policy.compaction_trigger(eff, policy.buffer_tokens)
    if not _policy.should_compact(int(prompt_tokens), trigger):
        return None, None
    if circuit_breaker.is_open:
        log.info(
            "compaction: circuit open (consecutive failures) - skip fold for conv %s",
            conversation_id,
        )
        return None, None

    keep = _policy.keep_budget(trigger, policy.keep_fraction, eff)
    rounds = group_into_rounds(rows)
    kept_rows, watermark = select_fold_rows(rows, rounds, keep)
    if watermark is None:
        return None, None  # keep budget covers everything; nothing to fold

    # Carry any previously stored fold summary forward: rows already compacted
    # (at or before the prior watermark) are summarized in conv["compaction_summary"],
    # so the new fold re-summarizes only the newly-folded rows plus that prior
    # summary instead of re-reading the whole transcript. First fold: no prior
    # summary, so the original single-fold path is preserved.
    prior_summary = conv.get("compaction_summary")
    fold_rows = [r for r in rows if r.get("id") is not None and r["id"] <= watermark]
    security = _security_instructions(fold_rows)
    prompt = build_summary_prompt(fold_rows, security, prior_summary)

    summary = await _summarize_with_degradation(
        model_client, prompt, model_override, cancel_ev
    )
    # #884: an empty or whitespace-only summary is a summarizer failure —
    # persisting it would let load_history drop the folded rows without
    # injecting any summary. Reject it before applying the fold.
    if summary is None or not summary.strip():
        circuit_breaker.record_failure()
        log.warning(
            "compaction: summary call failed (failure %d) - conv %s",
            circuit_breaker.consecutive_failures,
            conversation_id,
        )
        return None, None
    circuit_breaker.record_success()

    # Immutable apply: write the summary + watermark on the conversation row;
    # never touch the message rows. Reset context_tokens so the next call
    # re-measures (the folded rows are gone from replay).
    try:
        await update_conversation(
            conversation_id,
            touch_updated_at=False,
            compaction_summary=summary,
            compaction_watermark=int(watermark),
        )
        await set_conversation_usage(conversation_id, 0, model or None)
    except Exception as e:  # noqa: BLE001 - compaction must not break the turn
        log.warning("compaction_apply_failed: %s", e)
        return None, None

    if emit_status:
        emit_status(
            {
                "type": "compaction",
                "conversation_id": conversation_id,
                "context_window": window,
                "prompt_tokens": int(prompt_tokens),
                "keep_budget": keep,
                "watermark": int(watermark),
            }
        )
    log.info(
        "compaction: folded conv %s at watermark %d (keep %d tokens, trigger %d)",
        conversation_id,
        watermark,
        keep,
        trigger,
    )
    return summary, watermark


async def _summarize_with_degradation(
    model_client,
    prompt: list[dict],
    model_override: str | None,
    cancel_ev,
) -> str | None:
    """Summarize with progressive trimming if the request is too large.

    If the provider rejects the summary prompt for exceeding its context, trim
    the oldest fold text and retry, up to _MAX_TRIM_RETRIES times. Any non-token
    failure returns None (the caller records it on the circuit breaker). A user
    Stop during the summary cancels the request and propagates ModelCancelled
    (not a summarizer failure), so the turn can end cleanly.
    """
    # #884: the user message carries the fold text; trim by dropping the oldest
    # rounds' text (never the security-instructions header) until it fits.
    base = prompt
    for _ in range(_MAX_TRIM_RETRIES):
        try:
            return await _summary_call(
                model_client, base, model_override, cancel_ev
            )
        except ModelCancelled:
            # User hit Stop during the summary: propagate, do not record a
            # summarizer failure or persist a cancelled result.
            raise
        except ModelError as e:
            msg = str(e) or ""
            ceiling = _parse_token_gap(msg)
            if not ceiling:
                return None  # non-token failure: let the breaker handle it
            # #884: the ceiling is the model's maximum, not the amount to trim.
            # Trim the excess (how far the current request exceeds it) so the
            # next attempt actually shrinks past the limit. If the request size
            # can't be estimated, fall back to a bounded reduction.
            excess = _excess_tokens(base, ceiling)
            base = _trim_oldest_round(base, excess)
            if base is None:
                return None  # could not shrink further
    return None


def _excess_tokens(prompt: list[dict], ceiling: int) -> int:
    """How many tokens the current prompt exceeds the ceiling by.

    Rough: estimate the prompt size and subtract the provider's ceiling. If the
    estimate is unavailable, fall back to a bounded reduction of the ceiling so
    the retry still shrinks the request.
    """
    try:
        requested = sum(_estimate_tokens(str(r.get("content") or "")) for r in prompt)
        excess = requested - ceiling
        if excess > 0:
            return excess
    except Exception:  # noqa: BLE001 - estimation is best-effort
        pass
    return max(1, ceiling // 10)


_TOKEN_GAP_RE = re.compile(r"maximum of (\d+[\d,]*)", re.I)


def _parse_token_gap(message: str) -> int | None:
    """The provider's context ceiling from an overflow error, if present."""
    m = _TOKEN_GAP_RE.search(message)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _trim_oldest_round(prompt: list[dict], gap: int) -> list[dict] | None:
    """Drop the oldest fold text until the request is at least `gap` smaller."""
    user_msg = prompt[-1]
    text = user_msg["content"]
    body = text.split('"""')[1] if '"""' in text else text
    lines = body.splitlines()
    # Drop leading transcript lines (the oldest) until we've cut ~gap tokens.
    dropped = 0
    while lines and dropped < gap:
        lines.pop(0)
        dropped += _estimate_tokens(lines[0]) if lines else 0
    if not lines:
        return None
    new_user = {
        "role": "user",
        "content": f"Transcript to compress (truncated):\n\n\"\"\"\n"
        + "\n".join(lines)
        + "\n\"\"\"",
    }
    return [prompt[0], new_user]
