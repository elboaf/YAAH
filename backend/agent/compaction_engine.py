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
    """The readable text of a round, assistant content first then tool/user."""
    parts: list[str] = []
    for r in round_rows:
        role = r.get("role")
        content = r.get("content")
        if role == "assistant" and content:
            parts.append(content.strip())
        elif role in ("user", "tool") and content and not isinstance(content, list):
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
        round_tokens = sum(_estimate_tokens(_round_text(round_rows)) for _ in round_rows)
        # A round's own estimate is rough; use the newest row's id to mark the
        # watermark and the round's text for the fold.
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
    fold_rows: list[dict], security_instructions: list[str]
) -> list[dict]:
    """The tool-free summary call prompt.

    System: the compaction contract. User: the folded transcript (older turns)
    plus any verbatim security instructions.
    """
    fold_text = "\n\n".join(_round_text([r]) for r in fold_rows if r.get("content"))
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
    model_client, prompt: list[dict], model_override: str | None
) -> str:
    """One non-streaming, tool-free summary request; returns the summary text."""
    kwargs: dict = {"tools": None, "stream": False, "max_tokens": 2048}
    if model_override:
        kwargs["model"] = model_override
    kwargs["effort"] = None  # deterministic summary, no reasoning loop
    data = await model_client.chat(prompt, **kwargs)
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
    window = await _cw.get_context_window(
        model.split("::")[0] if "::" in model else model,
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

    fold_rows = [r for r in rows if r.get("id") is not None and r["id"] <= watermark]
    security = _security_instructions(fold_rows)
    prompt = build_summary_prompt(fold_rows, security)

    summary = await _summarize_with_degradation(
        model_client, prompt, model_override, cancel_ev
    )
    if summary is None:
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

    If the provider rejects the summary prompt for exceeding its context, drop
    the oldest fold and retry, up to _MAX_TRIM_RETRIES times. Any non-token
    failure returns None (the caller records it on the circuit breaker).
    """
    fold_rows = list(prompt[-1]["content"].split('"""')[1].splitlines())
    # Rebuild from the prompt: the user message carries the fold text; trim by
    # dropping the oldest rounds' text until it fits.
    base = prompt
    for _ in range(_MAX_TRIM_RETRIES):
        try:
            return await _summary_call(model_client, base, model_override)
        except ModelError as e:
            msg = str(e) or ""
            gap = _parse_token_gap(msg)
            if not gap:
                return None  # non-token failure: let the breaker handle it
            # Drop the oldest round's text from the fold to shrink the request.
            base = _trim_oldest_round(prompt, gap)
            if base is prompt:
                return None  # could not shrink further
    return None


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
