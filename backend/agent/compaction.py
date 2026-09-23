"""History compaction: keep per-call prompt size bounded on long sessions.

ZCode-style context management (adr/0004): when the conversation's
measured prompt size (the exact ``usage.prompt_tokens`` the provider
reported, persisted per conversation) crosses a fraction of the model's
context window, the oldest messages are folded into ONE summary and
replaced in the DB. The recent tail stays verbatim.

Design invariants:

- The trigger uses MEASURED tokens (never an estimate) against the
  resolved window (Settings override -> provider report -> built-in
  table). Unknown window falls back to a chars/4 estimate over a
  conservative default budget.
- The SPLIT POINT uses chars/4 estimates: only relative sizing matters
  there, and no tokenizer ships with the app.
- The cut boundary always lands on a user message, so the summarized
  prefix never breaks an assistant tool_calls -> tool result pair.
- Compaction is delete-not-tombstone: the folded rows are removed and
  one system row takes their place. ``load_history`` stays a pure
  replay with no compacted-row filtering, and the DB stays bounded
  like the context.
"""

import json
import logging
import re

from backend.agent import model_client
from backend.agent.config import load_config
from backend.agent.context_window import get_context_window

log = logging.getLogger("yaah.compaction")

# Master switch + knobs (config.json `compaction` section overrides;
# _compaction_cfg merges so partial configs keep the rest).
COMPACTION_ENABLED = True
# Fire when measured tokens exceed this fraction of the window. Headroom
# below 1.0 leaves room for the system prompt, tool plumbing, and a long
# reply without re-tripping the trigger on the very next call.
COMPACTION_TRIGGER_FRACTION = 0.70
# Compact DOWN to this fraction of the window: the recent tail stays
# verbatim, the summarized prefix carries the rest.
COMPACTION_KEEP_FRACTION = 0.40
# Never summarize the newest N messages of the loaded history, even if
# the token math would (the live end of the task must stay verbatim).
COMPACTION_MIN_TAIL_MESSAGES = 6
# Window unknown (not in the table, no provider report, no override):
# assume this so small local models still get protection.
COMPACTION_DEFAULT_WINDOW = 32_000
# chars -> tokens estimate for split-point math (~4 chars/token).
_EST_CHARS_PER_TOKEN = 4
# Hard caps for the summarizer call itself.
_TRANSCRIPT_CHAR_BUDGET = 60_000  # drop oldest excerpt items beyond this
_SUMMARY_MAX_CHARS = 12_000  # clamp a runaway summary
_ITEM_CHARS = 1_500  # per-message cap in the excerpt (tool results less)
_TOOL_RESULT_CHARS = 600
_TOOL_ARGS_CHARS = 200

_DEFAULTS = {
    "enabled": COMPACTION_ENABLED,
    "trigger_fraction": COMPACTION_TRIGGER_FRACTION,
    "keep_fraction": COMPACTION_KEEP_FRACTION,
    "keep_recent_messages": COMPACTION_MIN_TAIL_MESSAGES,
    "default_window": COMPACTION_DEFAULT_WINDOW,
    # "" = summarize with the session's own provider/model; or a
    # "provider::model" / bare model id to route the summary call
    # somewhere cheaper.
    "model": "",
}


def _compaction_cfg() -> dict:
    """Effective compaction settings: DEFAULTS merged with the user's
    config.json `compaction` section (partial sections keep defaults)."""
    cfg = dict(_DEFAULTS)
    section = load_config().get("compaction") or {}
    if isinstance(section, dict):
        cfg.update(section)
    # Clamp numeric knobs into sane ranges; garbage falls back.
    try:
        cfg["trigger_fraction"] = min(max(float(cfg["trigger_fraction"]), 0.1), 1.0)
    except (TypeError, ValueError):
        cfg["trigger_fraction"] = COMPACTION_TRIGGER_FRACTION
    try:
        cfg["keep_fraction"] = min(max(float(cfg["keep_fraction"]), 0.05), 0.95)
    except (TypeError, ValueError):
        cfg["keep_fraction"] = COMPACTION_KEEP_FRACTION
    try:
        cfg["keep_recent_messages"] = max(int(cfg["keep_recent_messages"]), 2)
    except (TypeError, ValueError):
        cfg["keep_recent_messages"] = COMPACTION_MIN_TAIL_MESSAGES
    try:
        cfg["default_window"] = max(int(cfg["default_window"]), 4_000)
    except (TypeError, ValueError):
        cfg["default_window"] = COMPACTION_DEFAULT_WINDOW
    return cfg


def estimate_tokens(messages: list) -> int:
    """chars/4 estimate over message contents (split-point math only;
    the trigger uses the provider's measured prompt_tokens)."""
    total = 0
    for m in messages:
        total += len(_content_text(m.get("content"))) + 8
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
    return total // _EST_CHARS_PER_TOKEN


def _content_text(content) -> str:
    """Plain text out of a message content (str, or an OpenAI parts list
    with image parts marked)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
                elif p.get("type") == "image_url":
                    parts.append("[image]")
                else:
                    parts.append(str(p))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content)


async def resolve_window(model: str | None, cfg: dict | None = None) -> int:
    """Resolved context window for the model, falling back to the
    configured default when nothing better exists. Accepts
    "provider::model" overrides and strips the provider prefix (the
    window is a property of the model, not the route)."""
    ccfg = _compaction_cfg()
    if model:
        bare = model.partition("::")[2] if "::" in model else model
        window = await get_context_window(bare, cfg or load_config())
        if window:
            return int(window)
    return ccfg["default_window"]


def should_compact(context_tokens: int | None, context_window: int) -> bool:
    """Measured prompt size vs the trigger fraction of the window."""
    ccfg = _compaction_cfg()
    if not ccfg["enabled"] or not context_tokens or not context_window:
        return False
    return context_tokens > context_window * ccfg["trigger_fraction"]


def find_cut_index(messages: list, keep_tokens: int, min_tail: int = 0) -> int:
    """Index of the first message of the VERBATIM TAIL: messages[:cut]
    are the compaction candidates.

    Walks from the end backwards until ~keep_tokens are gathered, then
    slides the boundary LEFT to the nearest user message so the summary
    never splits an assistant tool_calls -> tool pair. Returns 0 when
    there is nothing safe to summarize (too little history, or every
    boundary candidate would leave less than min_tail messages).
    """
    n = len(messages)
    if n <= min_tail:
        return 0
    acc = 0
    cut = n
    for i in range(n - 1, -1, -1):
        m = messages[i]
        acc += len(_content_text(m.get("content"))) + 8
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            acc += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
        if acc >= keep_tokens * _EST_CHARS_PER_TOKEN:
            cut = i + 1
            break
    else:
        return 0  # whole history fits the keep budget: nothing to fold
    # Slide left to a user boundary (a summary that begins mid-pair is
    # API-invalid once replayed). cut == n means the walk ate everything:
    # the boundary is virtual, so step back from the end.
    while cut > 0 and (cut >= n or messages[cut].get("role") != "user"):
        cut -= 1
    # No user boundary found, or the tail would dip below the floor:
    # keep everything verbatim this turn.
    if cut <= 0 or n - cut < min_tail:
        return 0
    return cut


def _excerpt(cut_messages: list) -> str:
    """Rendered transcript of the to-be-summarized prefix, with per-item
    truncation and an overall budget (oldest items drop first — the
    recent end of the prefix matters most for continuity)."""
    lines: list[str] = []
    for m in cut_messages:
        role = m.get("role", "?")
        tcs = m.get("tool_calls") or []
        if role == "assistant" and tcs:
            calls = "; ".join(
                (tc.get("function") or {}).get("name", "?")
                + f"({str((tc.get('function') or {}).get('arguments') or '')[:_TOOL_ARGS_CHARS]})"
                for tc in tcs
            )
            text = _content_text(m.get("content"))
            body = f"[tool calls: {calls}]" + (f" {text[:_ITEM_CHARS]}" if text else "")
        elif role == "tool":
            tc_id = m.get("tool_call_id") or ""
            body = f"[tool result {tc_id}] {_content_text(m.get('content'))[:_TOOL_RESULT_CHARS]}"
        else:
            body = _content_text(m.get("content"))[:_ITEM_CHARS]
        lines.append(f"{role}: {body}")
    while len("\n".join(lines)) > _TRANSCRIPT_CHAR_BUDGET and len(lines) > 8:
        lines.pop(0)
    return "\n".join(lines)


_SUMMARIZER_PROMPT = (
    "You compress the earlier portion of an AI agent session into a compact "
    "continuity summary. A new assistant instance will continue the session "
    "seeing ONLY your summary in place of this excerpt.\n"
    "Preserve: the user's goals and explicit instructions; decisions made and "
    "why; concrete anchors — file paths, branch names, commands, "
    "function/variable names, error messages; what was attempted and what "
    "failed; the current state and obvious next steps.\n"
    "Write at most 400 words, plain prose or terse bullets. Reply with JSON "
    "only, no fences: {\"summary\": \"...\"}"
)


async def summarize_messages(cut_messages: list, model_override: str = "") -> str:
    """One tool-free, non-streaming model call over the excerpt. Returns
    the summary text (never empty — falls back to a truncated raw replay
    if the model misbehaves)."""
    excerpt = _excerpt(cut_messages)
    messages = [
        {"role": "system", "content": _SUMMARIZER_PROMPT},
        {"role": "user", "content": excerpt},
    ]
    data = await model_client.chat(
        messages, tools=None, stream=False, model=model_override or ""
    )
    try:
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except AttributeError:
        content = ""
    text = str(content).strip()
    summary = _extract_summary(text)
    if not summary:
        # No JSON envelope: accept substantive unfenced prose (a weaker
        # model that summarized without following the format). Short
        # filler/refusals get the clipped verbatim excerpt instead — the
        # prefix is never lost to an "I don't know" reply.
        summary = text if len(text) >= 80 else excerpt[:4_000]
    return summary[:_SUMMARY_MAX_CHARS]


def _extract_summary(text: str) -> str:
    """Pull the `summary` string out of a possibly-fenced JSON reply;
    empty string when the reply carries none."""
    if not text:
        return ""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return ""
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict):
        s = parsed.get("summary")
        if isinstance(s, str) and s.strip():
            return s.strip()
    return ""


async def compact_history_for_context(
    conversation_id: int,
    model_window: int | None = None,
    *,
    model_id: str | None = None,
) -> dict | None:
    """The whole pass, run once per turn before the first model call:
    measured tokens vs window -> cut -> summarize -> persist.

    Returns a `compacted` event payload when compaction happened, else
    None. Never raises: any failure logs and leaves the conversation
    untouched — a skipped compaction only costs tokens, not the turn.
    """
    from backend.db import database as db

    ccfg = _compaction_cfg()
    if not ccfg["enabled"]:
        return None
    conv = await db.get_conversation(conversation_id)
    if not conv:
        return None
    cfg = load_config()
    window = model_window or await resolve_window(model_id or cfg.get("model"), cfg)
    measured = conv.get("context_tokens")
    if not should_compact(measured, window):
        return None

    rows = await db.get_messages(conversation_id)
    # The cut is computed over the RAW persisted rows (not the repaired
    # replay load_history produces): compact_conversation deletes a
    # prefix of the table, so the boundary must be expressed in the
    # same units. Repair deltas (dropped orphan rows, synthetic tool
    # answers) are rare and don't move the boundary meaningfully.
    keep_budget = int(window * ccfg["keep_fraction"])
    cut = find_cut_index(rows, keep_budget, ccfg["keep_recent_messages"])
    if cut <= 0:
        log.info(
            "compaction skipped for conv %s: no safe cut (%d msgs, %s/%s tokens)",
            conversation_id, len(rows), measured, window,
        )
        return None

    cut_messages = rows[:cut]
    summary = await summarize_messages(cut_messages, ccfg.get("model") or "")
    removed = await db.compact_conversation(
        conversation_id, summary, cut_messages=len(cut_messages)
    )
    if not removed:
        return None
    log.info(
        "compacted conv %s: %d messages folded (%s -> est %s of %s tokens)",
        conversation_id, cut, measured, estimate_tokens(rows[cut:]), window,
    )
    return {
        "type": "compacted",
        "summarized_messages": removed,
        "summary": summary,
        "estimated_tokens_before": int(measured or 0),
        "context_window": int(window),
    }
