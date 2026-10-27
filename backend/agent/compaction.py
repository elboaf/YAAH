"""Prompt compaction: bound model context without changing the transcript.

When measured prompt usage crosses the configured threshold, a cumulative
summary is stored separately from the conversation's immutable transcript.
The summary watermark controls model replay; transcript reads, exports, and
agent search retain access to the original messages.
"""

import asyncio
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
# Absolute trigger (tokens): fire when measured tokens exceed this value.
# Purely a token threshold — no fraction-of-window component. The per-model
# Settings editor overrides this per model (300k shown as the default).
COMPACTION_TRIGGER_TOKENS = 0
# Models WITHOUT a per-model Settings entry: the trigger is this fraction
# of the DETECTED context window (#578) — the shipped 300k absolute token
# default never fires for a 65k-window local model, so those conversations
# grew unbounded. Explicit per-model entries (and a positive global
# compaction.trigger_tokens) keep their exact absolute semantics.
COMPACTION_WINDOW_FRACTION = 0.80
# Floor for the derived trigger so a tiny detected window cannot make
# compaction churn on every other turn.
COMPACTION_DERIVED_MIN_TOKENS = 8_000
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
    "trigger_tokens": COMPACTION_TRIGGER_TOKENS,
    # "enabled"/"trigger_tokens" defaults for models without a per-model
    # entry in config.model_compaction (trigger in absolute tokens; 300k
    # shown and used as the shipped default by the Settings editor).
    "per_model_enabled": True,
    "per_model_trigger_tokens": 300_000,
    "model_compaction": {},
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
        cfg["trigger_tokens"] = max(int(cfg["trigger_tokens"] or 0), 0)
    except (TypeError, ValueError):
        cfg["trigger_tokens"] = COMPACTION_TRIGGER_TOKENS
    pm = load_config().get("model_compaction") or {}
    cfg["model_compaction"] = pm if isinstance(pm, dict) else {}
    return cfg


def _compaction_cfg_for_model(bare_model: str, ccfg: dict, context_window: int | None = None) -> dict:
    """Compaction settings for one bare model id: the per-model entry
    (config.model_compaction) when present, else the shipped defaults.

    `context_window` (when the caller already resolved it) only affects
    the NO-ENTRY fallback: instead of the flat 300k default — which can
    exceed a local model's real window and so never fire (#578) — the
    trigger derives from the window (COMPACTION_WINDOW_FRACTION). An
    explicit per-model entry keeps its exact absolute semantics.

    COMPACTION_DEFAULT_WINDOW is resolve_window's WORKING default for cut
    math, not a detection: passing it (like None/0) means unknown, so an
    undetected model keeps the 300k trigger instead of deriving from the
    32k guess (CodeRabbit round on PR #579).
    """
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
    # No per-model entry: a configured global trigger_tokens (> 0) applies;
    # otherwise the trigger derives from the model's detected window (or,
    # when the window is unknown, the shipped 300k default — same as
    # before #578).
    try:
        cfg["trigger_tokens"] = int(ccfg.get("trigger_tokens") or 0)
    except (TypeError, ValueError):
        cfg["trigger_tokens"] = 0
    if cfg["trigger_tokens"] <= 0:
        derived = 0
        if context_window and context_window != COMPACTION_DEFAULT_WINDOW:
            derived = int(context_window * COMPACTION_WINDOW_FRACTION)
            derived = max(derived, COMPACTION_DERIVED_MIN_TOKENS)
        if derived > 0:
            cfg["trigger_tokens"] = derived
        else:
            try:
                cfg["trigger_tokens"] = max(int(ccfg.get("per_model_trigger_tokens") or 0), 0)
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


def split_model_route(model: str, cfg: dict) -> tuple[str, str, str]:
    """(bare model id, api_base, api_key) for a model id: "provider::model"
    probes the OWNING provider's endpoint; a bare id probes the active
    provider. Unknown provider names degrade to the active provider's
    endpoint (the pre-#578 behavior)."""
    if "::" in model:
        provider, bare = model.split("::", 1)
        p = (cfg.get("providers") or {}).get(provider)
        if p:
            return bare, p.get("api_base") or "", p.get("api_key") or ""
        return bare, cfg.get("api_base") or "", cfg.get("api_key") or ""
    return model, cfg.get("api_base") or "", cfg.get("api_key") or ""


async def resolve_window(model: str | None, cfg: dict | None = None) -> int:
    """Resolved context window for the model, falling back to the
    configured default when nothing better exists. Accepts
    "provider::model" overrides: the window is a property of the model,
    but the probe (router /v1/models, Ollama /api/show) must hit the
    OWNING provider's base, not whichever provider happens to be active."""
    ccfg = _compaction_cfg()
    if model:
        bare, api_base, api_key = split_model_route(model, cfg or load_config())
        window = await get_context_window(
            bare, cfg or load_config(), api_base=api_base, api_key=api_key
        )
        if window:
            return int(window)
    return COMPACTION_DEFAULT_WINDOW


def should_compact(context_tokens: int | None, context_window: int, model: str | None = None) -> bool:
    """Measured prompt size vs the absolute `trigger_tokens` threshold.

    Purely a token value (no fraction-of-window component): compaction
    fires when measured tokens exceed trigger_tokens. `context_window`
    guards the call (0 = unknown window, don't fire) AND feeds the
    no-entry trigger derivation (80% of the window, #578) — so an
    unconfigured model with a detected window compacts at a sane
    fraction of what it can actually hold, instead of at a flat 300k
    it can never reach. COMPACTION_DEFAULT_WINDOW counts as unknown for
    the derivation (resolve_window's working default is not a detection,
    CodeRabbit round on PR #579).

    `model` (bare id) selects the per-model settings when present;
    without it the global compaction block applies.
    """
    ccfg = _compaction_cfg()
    if model:
        ccfg = _compaction_cfg_for_model(model, ccfg, context_window)
    if not ccfg["enabled"] or not context_tokens or not context_window:
        return False
    absolute = int(ccfg.get("trigger_tokens") or 0)
    return absolute > 0 and context_tokens > absolute


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


_SUMMARIZER_PROMPT = """You compress the earlier portion of an AI agent session into a compact continuity summary. The conversation transcript remains preserved for review and search; this summary is only a bounded replacement in future model prompts. Write your reply as a structured document with exactly these eight section headings, in this order. A section with nothing to say says `None` (or `N/A`) rather than being dropped.

## Next focus
What the next session/turn is for. Carry the continuation plan forward.

## Goals and explicit instructions
The user's stated goals, constraints, verbatim asks.

## Decisions made
Decisions the session made, with the why.

## Attempts and failures
What was tried, what failed, error messages.

## Concrete anchors
File paths, branch names, commands, function/variable names, error strings.

## Artifact pointers
Specs, ADRs, issues, commits, diffs referenced by path or URL — do not
duplicate content that already lives in those artifacts.

## Suggested skills
Which skills the next context should load.

## Redaction note
What was redacted, or `None`.

Each section uses terse bullets in plain style.

When an earlier summary is included in the input, preserve its existing
section content and *add* new facts to the relevant sections. Do not
replace earlier content — fold new facts into the existing sections.

Write at most 600 words of terse bullets (the section shape bounds
length naturally; the platform may keep up to 12,000 characters).
Reply with the 8 section headings directly as markdown; a bare JSON
envelope {"summary": "..."} is also accepted."""

async def summarize_messages(
    cut_messages: list, model_override: str = "", cancel_event=None
) -> str:
    """One tool-free, non-streaming model call over the excerpt. Returns
    the summary text (never empty — falls back to a truncated raw replay
    if the model misbehaves). cancel_event: Stop event (#389) — when it
    fires mid-call the request task is cancelled and ModelCancelled
    propagates to the turn's settle path."""
    excerpt = _excerpt(cut_messages)
    messages = [
        {"role": "system", "content": _SUMMARIZER_PROMPT},
        {"role": "user", "content": excerpt},
    ]
    from backend.agent import loop as _loop

    data = await _loop._race_model_call(
        asyncio.ensure_future(
            model_client.chat(
                messages, tools=None, stream=False, model=model_override or ""
            )
        ),
        cancel_event,
    )
    try:
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except AttributeError:
        content = ""
    text = str(content).strip()
    summary = _extract_summary(text)
    if not summary:
        # Extraction found nothing. A reply that carries section headings
        # but was rejected as malformed must not become the persisted
        # summary: keep the verbatim excerpt instead. Plain unheaded
        # prose stays on the >=80-char substantive fallback (a weaker
        # model that summarized without following the format); short
        # filler/refusals get the excerpt too — the prefix is never
        # lost to an "I don't know" reply.
        if re.search(r"^##\s+", text, re.MULTILINE):
            summary = excerpt[:4_000]
        else:
            summary = text if len(text) >= 80 else excerpt[:4_000]
    return summary[:_SUMMARY_MAX_CHARS]


def _extract_summary(text: str) -> str:
    """Pull the summary out of a model reply: the raw 8-section markdown
    document (the #563 shape) or a JSON envelope; "" when neither is
    recognized (the caller then substitutes excerpt/prose fallbacks).

    Any reply carrying a ## section heading is treated as the sectioned
    document — including one that leads with a JSON blob: sections after
    {"summary": ...} are newer content and must not be dropped. A
    degenerate headed reply (a heading but too little content) returns ""
    so the caller keeps the excerpt. The JSON path fires only for
    envelope-style replies with no headings anywhere — the whole reply
    is the object, optionally fenced. Shape is requested, not
    gate-kept: unheaded prose is the caller's fallback, not an error."""
    if not text:
        return ""

    def _markdown_doc() -> str:
        """The sectioned document (from the first ## heading on), or ""
        when the headed content is too short to be a real summary."""
        h = re.search(r"^##\s+", text, re.MULTILINE)
        if not h:
            return ""
        doc = text[h.start():].strip()
        return doc if len(doc) >= 80 else ""

    if re.search(r"^##\s+", text, re.MULTILINE):
        # Headed reply: always the sectioned document, even when a JSON
        # blob precedes it.
        return _markdown_doc()
    stripped = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    if not stripped.startswith("{"):
        # Unheaded, non-envelope: caller applies the prose fallback.
        return ""
    # JSON envelope (pre-#563 format) — extract only when it parses.
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            s = parsed.get("summary")
            if isinstance(s, str) and s.strip():
                return s.strip()
    # Unparseable envelope or missing summary key: nothing recognized.
    return ""


async def compact_history_for_context(
    conversation_id: int,
    model_window: int | None = None,
    *,
    model_id: str | None = None,
    cancel_event: asyncio.Event | None = None,
) -> dict | None:
    """The whole pass, run once per turn before the first model call:
    measured tokens vs window -> cut -> cumulatively summarize -> persist
    prompt-only state, leaving every transcript message untouched.

    cancel_event: the turn's Stop event (#389) — raced against the
    summarizer's model call so Stop cuts a slow compaction too.

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
    # The resolved window feeds the NO-ENTRY trigger fallback (80% of the
    # detected window instead of the flat 300k default, #578) — resolve it
    # BEFORE the per-model pass so the derivation can use it.
    window = model_window or await resolve_window(model_id or cfg.get("model"), cfg)
    if bare:
        ccfg = _compaction_cfg_for_model(bare, ccfg, window)
        if not ccfg["enabled"]:
            return None
    measured = conv.get("context_tokens")
    if not should_compact(measured, window, bare or None):
        return None

    rows = await db.get_messages(conversation_id)
    state = await db.get_prompt_summary(conversation_id)
    watermark = int(state.get("through_message_id") or 0)
    # Only unsummarized transcript rows participate in the next cut. The
    # transcript itself remains untouched; the watermark affects prompt replay.
    pending = [r for r in rows if r["id"] > watermark]
    keep_budget = int(window * COMPACTION_KEEP_FRACTION)
    cut = find_cut_index(pending, keep_budget, COMPACTION_MIN_TAIL_MESSAGES)
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
    # Back-compat: only pass cancel_event when there is one — callers (and
    # test fakes) with the old signature keep working when cancellation is
    # not wired.
    if cancel_event is not None:
        summary = await summarize_messages(
            summary_input, ccfg.get("model") or "", cancel_event=cancel_event
        )
    else:
        summary = await summarize_messages(summary_input, ccfg.get("model") or "")
    if cancel_event is not None and cancel_event.is_set():
        # Stop fired between the summarizer and the DB write (#389):
        # persist nothing — a cancelled turn must not mutate prompt state.
        raise model_client.ModelCancelled("cancelled by user")
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
