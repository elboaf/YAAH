"""Prompt compaction: bound model context without changing the transcript.

When measured prompt usage crosses the configured threshold, a cumulative
summary is stored separately from the conversation's immutable transcript.
The summary watermark controls model replay; transcript reads, exports, and
agent search retain access to the original messages.
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
# Optional ABSOLUTE trigger (tokens): when > 0, compaction fires at
# min(trigger_tokens, window * trigger_fraction) — a big-window model
# waits until the absolute threshold, while a small-window model still
# compacts before overflowing. 0 = fraction-only (legacy behavior).
# Legacy absolute default: 0 = fraction-of-window only. The per-model
# Settings editor overrides this per model (300k shown as the default).
COMPACTION_TRIGGER_TOKENS = 0
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
    "trigger_tokens": COMPACTION_TRIGGER_TOKENS,
    # "enabled"/"trigger_tokens" defaults for models without a per-model
    # entry in config.model_compaction (trigger in absolute tokens; 300k
    # shown and used as the shipped default by the Settings editor).
    "per_model_enabled": True,
    "per_model_trigger_tokens": 300_000,
    "model_compaction": {},
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
        cfg["trigger_tokens"] = max(int(cfg["trigger_tokens"] or 0), 0)
    except (TypeError, ValueError):
        cfg["trigger_tokens"] = COMPACTION_TRIGGER_TOKENS
    try:
        cfg["keep_recent_messages"] = max(int(cfg["keep_recent_messages"]), 2)
    except (TypeError, ValueError):
        cfg["keep_recent_messages"] = COMPACTION_MIN_TAIL_MESSAGES
    try:
        cfg["default_window"] = max(int(cfg["default_window"]), 4_000)
    except (TypeError, ValueError):
        cfg["default_window"] = COMPACTION_DEFAULT_WINDOW
    pm = load_config().get("model_compaction") or {}
    cfg["model_compaction"] = pm if isinstance(pm, dict) else {}
    return cfg


def _compaction_cfg_for_model(bare_model: str, ccfg: dict) -> dict:
    """Compaction settings for one bare model id: the per-model entry
    (config.model_compaction) when present, else the shipped defaults."""
    entry = (ccfg.get("model_compaction") or {}).get(bare_model)
    if isinstance(entry, dict):
        cfg = dict(ccfg)
        cfg["enabled"] = bool(entry.get("enabled", True))
        try:
            cfg["trigger_tokens"] = max(int(entry.get("trigger_tokens") or 0), 0)
        except (TypeError, ValueError):
            cfg["trigger_tokens"] = 0
        return cfg
    cfg = dict(ccfg)
    cfg["enabled"] = bool(ccfg.get("per_model_enabled", True))
    try:
        cfg["trigger_tokens"] = max(
            int(ccfg.get("per_model_trigger_tokens") or 0), 0
        )
    except (TypeError, ValueError):
        cfg["trigger_tokens"] = 300_000
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


def should_compact(context_tokens: int | None, context_window: int, model: str | None = None) -> bool:
    """Measured prompt size vs the trigger threshold.

    With an absolute `trigger_tokens` set (> 0), the trigger is
    min(trigger_tokens, window * trigger_fraction): big-window models
    wait for the absolute threshold, small-window models still compact
    before overflowing. Without it, fraction-of-window only.

    `model` (bare id) selects the per-model settings when present;
    without it the global compaction block applies.
    """
    ccfg = _compaction_cfg()
    if model:
        ccfg = _compaction_cfg_for_model(model, ccfg)
    if not ccfg["enabled"] or not context_tokens or not context_window:
        return False
    trigger = context_window * ccfg["trigger_fraction"]
    absolute = int(ccfg.get("trigger_tokens") or 0)
    if absolute > 0:
        trigger = min(trigger, absolute)
    return context_tokens > trigger


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
        elif role in {"previous summary", "legacy prompt summary"}:
            body = _content_text(m.get("content"))[:_SUMMARY_MAX_CHARS]
        else:
            body = _content_text(m.get("content"))[:_ITEM_CHARS]
        lines.append(f"{role}: {body}")
    while len("\n".join(lines)) > _TRANSCRIPT_CHAR_BUDGET and len(lines) > 8:
        # If a prior summary is present at the end, preserve it while the
        # oldest newly folded transcript items are trimmed from the front.
        if len(lines) > 1 and cut_messages[-1].get("role") == "previous summary":
            lines.pop(-2)
        else:
            lines.pop(0)
    return "\n".join(lines)


_SUMMARIZER_PROMPT = (
    "You compress the earlier portion of an AI agent session into a compact "
    "continuity summary. The conversation transcript remains preserved for "
    "review and search; this summary is only a bounded replacement in future "
    "model prompts. If an earlier summary is included, merge its important "
    "facts with the newly summarized messages.\n"
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
    measured tokens vs window -> cut -> cumulatively summarize -> persist
    prompt-only state, leaving every transcript message untouched.

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
    if model_id:
        bare = model_id.partition("::")[2] if "::" in model_id else model_id
    else:
        cfg_model = cfg.get("model") or ""
        bare = cfg_model.partition("::")[2] if "::" in cfg_model else cfg_model
    # Per-model settings: the model's own entry decides on/off + trigger.
    if bare:
        ccfg = _compaction_cfg_for_model(bare, ccfg)
        if not ccfg["enabled"]:
            return None
    window = model_window or await resolve_window(model_id or cfg.get("model"), cfg)
    measured = conv.get("context_tokens")
    if not should_compact(measured, window, bare or None):
        return None

    rows = await db.get_messages(conversation_id)
    state = await db.get_prompt_summary(conversation_id)
    watermark = int(state.get("through_message_id") or 0)
    # Only unsummarized transcript rows participate in the next cut. The
    # transcript itself remains untouched; the watermark affects prompt replay.
    pending = [r for r in rows if r["id"] > watermark]
    keep_budget = int(window * ccfg["keep_fraction"])
    cut = find_cut_index(pending, keep_budget, ccfg["keep_recent_messages"])
    if cut <= 0:
        log.info(
            "compaction skipped for conv %s: no safe cut (%d pending msgs, %s/%s tokens)",
            conversation_id, len(pending), measured, window,
        )
        return None

    cut_messages = pending[:cut]
    through_message_id = int(cut_messages[-1]["id"])
    summary_input = list(cut_messages)
    prior_summary = state.get("summary") or ""
    if watermark and not prior_summary:
        for row in rows:
            if row["role"] == "system" and int(row["id"]) == watermark:
                prior_summary = row["content"]
                break
    # Put the prior summary last so _excerpt's oldest-first budget trimming
    # cannot discard the continuity facts needed for cumulative compaction.
    if prior_summary:
        summary_input.append({"role": "previous summary", "content": prior_summary})
    summary = await summarize_messages(summary_input, ccfg.get("model") or "")
    summarized = await db.compact_conversation(
        conversation_id, summary, through_message_id=through_message_id
    )
    if not summarized:
        return None
    log.info(
        "compacted conv %s: %d messages summarized through id %d (%s -> est %s of %s tokens)",
        conversation_id, summarized, through_message_id, measured,
        estimate_tokens(pending[cut:]), window,
    )
    return {
        "type": "compacted",
        "summarized_messages": summarized,
        "summary": summary,
        "estimated_tokens_before": int(measured or 0),
        "context_window": int(window),
    }
