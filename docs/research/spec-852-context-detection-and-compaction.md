# Spec: Context detection + history compaction (ports zcode) — issue #852

> Synthesized from issue #852 ("blatently rip off zcode's context detection and
> history compact methods") plus the repo's ADR 0004 (the compaction feature that
> was shipped) and ADR 0020 (the same feature, removed entirely after six rounds
> of failures). `zcode` here is `zai-org/ZCode` — Z.ai's GLM harness (confirmed in
> `docs/research/zcode-evaluation.md`); it is **not** the unrelated `zerx-lab/zcode`
> terminal agent.

## Problem Statement

YAAH replays the full persisted transcript on every turn (`load_history` → the
model call). Tool results are clipped at write time, images are pruned, and
sub-agent transcripts stay out of the parent context, but the **text history
grows without bound**. A long session keeps getting more expensive per turn and
eventually hard-fails at the model's context ceiling with a 400.

Compaction previously existed (ADR 0004) and was removed entirely (ADR 0020,
driven by #620) because it failed across six rounds of fixes: trigger misses
under per-model context settings, mid-turn 400s before any turn-start pass could
see the growth, summarizer cancellations and churn, cache-invalidation cost on
every fold, and repeated regressions in the cut/keep math. The owner decided to
remove the feature rather than attempt a seventh repair.

Issue #852 asks to re-close this gap by adopting zcode's context-detection and
history-compaction engine — the same problem, a different (and, per the issue's
own research, more robust) approach.

## Solution

Re-introduce **auto-compaction** on top of YAAH's existing context detection,
using zcode's engine. Context *detection* already exists in
`backend/agent/context_window.py` (`get_context_window`, `split_model_route`);
what is missing is the **compaction policy and the summarization/teardown
pass**. The port covers:

1. **Output-reserve budget.** The effective input budget is
   `contextWindow − maxOutputTokens − safetyMargin`, not the full window. The
   trigger fires against this effective window, and output is capped at
   `min(baselineMaxOutputTokens, effective − buffer)` so the reply always has
   room. (ADR 0004 already triggered on measured tokens; this adds the output
   reserve that the removed feature lacked.)
2. **Measured-token trigger with a circuit breaker.** Compaction fires when the
   DB-stored `usage.prompt_tokens` (exact, provider-reported, already persisted
   via `set_conversation_usage` in `loop.py`) exceeds `effectiveWindow −
   bufferTokens`. After a small number of consecutive failures the circuit
   opens and auto-compaction stops rather than looping.
3. **Round-based message selection.** Messages are grouped into assistant-started
   rounds; the most recent round(s) are preserved verbatim and older rounds are
   folded into a summary. The cut point never straddles a user/tool pair.
4. **Structured, tool-free summary prompt.** The summary call is forced to plain
   text (`<analysis>` then `<summary>` blocks, no tools), enumerates
   continuity-critical sections (intent, files/code, errors, pending tasks,
   current work, next step with verbatim quotes), and copies
   security-relevant instructions verbatim so they survive compaction.
5. **Graceful degradation.** If the summary request itself exceeds the window,
   retry with progressively more aggressive trimming (preserve more recent
   groups, then drop oldest rounds), parsing the provider's token-gap from its
   error message; surface a clear error only if all retries fail.
6. **Incremental micro-compaction.** A cheap first line that clears old
   tool-result *content* (replaced with a placeholder) rather than invoking a
   model summary — triggered by idle time or token pressure, keeping the most
   recent results verbatim, skipping errors and media, and gated behind a minimum
   token-savings threshold.

The parts of ADR 0004 that worked are preserved unchanged: the transcript is
**immutable** by compaction (summary + watermark on the conversation, never a
message-row edit), replay injects the summary and skips rows through the
watermark, review/export/search read the original messages, and the user is told
via a status chip. **Every one of the six ADR-0020 failure modes has a direct
mitigation in the list above** — this mapping is the whole point of the port and
is called out explicitly under Implementation Decisions.

## User Stories

1. As a user running a long single-session task, I want my per-turn prompt size
   to plateau instead of growing linearly, so that I don't pay more for every
   additional turn.
2. As a user on a long session, I want to stop seeing the model's context-ceiling
   400 errors, so that my work isn't interrupted mid-turn.
3. As a user, I want the most recent turns to remain fully visible to the model
   verbatim, so that recent context isn't degraded by summarization.
4. As a user, I want older turns to be summarized rather than dropped, so that I
   retain continuity (what was decided, what's pending) across a long session.
5. As a user, I want security-relevant instructions I gave early in a session
   (e.g. "never edit file X", "this is a read-only workspace") to stay in effect
   after compaction, so that a summary doesn't silently lift a guard rail.
6. As a user, I want to still be able to review, export, and search the full
   original transcript after compaction, so that compaction is lossless from my
   point of view.
7. As a user, I want a visible status chip when a session is compacted, so that I
   know the harness is managing my context rather than silently changing it.
8. As a user with a large-context window model, I want compaction to fire at a
   sensible fraction of the window rather than at the hard ceiling, so that I
   get the cost benefit without waiting until the model is about to fail.
9. As a user with a small/local model, I want compaction to still engage before
   the (small) window is exhausted, so that small models get the same protection.
10. As a user, I want compaction to respect a per-model trigger I can configure,
    so that I can tune aggressiveness per model.
11. As a user, I want the effective "fires at Xk" behavior visible in Settings
    and on the context dial, so that I'm not guessing whether compaction is on.
12. As a user, I want compaction to degrade gracefully when the summary request
    is itself too large, so that a single pathological turn doesn't crash the
    session.
13. As a user, I want repeated failed compactions to stop being retried
    indefinitely (a circuit breaker), so that a broken summarizer can't wedge a
    session.
14. As a user whose session goes idle for a long time, I want cheap cleanup of
    stale tool-result content to happen automatically, so that a resumed session
    starts leaner without waiting for a full summary.
15. As a user, I want micro-compaction to never delete or alter my original
    messages, only to clear their content in the replayed prompt, so that my
    transcript stays intact.
16. As a user, I want the compaction summary call to be billed/counted correctly
    (or explicitly excluded from step counts), so that the extra model call doesn't
    distort my usage accounting.
17. As a user, I want short chats to be completely unaffected — compaction should
    be unreachable until context is actually large — so that I see zero behavior
    change on normal short sessions.
18. As a scheduled-agent owner with memory enabled, I want the same context
    protection applied to my pinned conversations on their next fire, so that
    long-running scheduled tasks don't blow up their context.
19. As a user, I want compaction to preserve the cut at a message boundary
    (never mid user/tool pair), so that the prompt handed to the model is
    API-valid.
20. As a user, I want the effective budget to reserve output space for the reply
    before it's generated, so that the request can't overflow just because the
    reply is long.
21. As a user switching models mid-session, I want the trigger to move with the
    newly resolved context window, so that a model change doesn't leave
    compaction firing at the wrong size.
22. As a user, I want the summary to capture the explicit "next step" with a
    verbatim quote from the recent conversation, so that the model doesn't drift
    off course after a long summary.

## Implementation Decisions

**Seam.** The single highest seam is the **assembled prompt handed to
`model_client.chat`**: given a conversation and a model call, the prompt must be
bounded by the model's effective context window, preserve the most recent turns
verbatim, represent older turns as one bounded summary, and preserve
security-relevant instructions verbatim. Everything else (budget math, the
summary call, watermark injection at replay) is a lower seam that this black-box
behavior pins down. Reuse existing seams rather than introduce new ones:
`context_window.py` for detection, the model-call loop in `loop.py` for the
pre-call compaction pass, and `load_history` for watermark-based replay.

**Modules touched.**
- `context_window.py` — add the output-reserve effective-window computation and
  the auto-compact policy (threshold, buffer, circuit-breaker state).
- The model-call loop (`loop.py`) — the pre-call compaction pass: measure
  `usage.prompt_tokens`, decide via the policy, run round-based selection, emit
  the tool-free summary call, apply the summary + watermark, and emit the status
  event. Guard with a monotonic per-conversation gate (one compaction per turn,
  never twice on the same `context_tokens`).
- `load_history` / prompt replay — inject the summary and skip rows past the
  watermark; leave transcript review/export/search reading original messages.
- `loop.py` `_record_call_usage` path — keep `context_tokens` null after a
  compaction so the next call re-measures; keep the summary call excluded from
  usage persistence and step counts (as ADR 0004 specified).
- Settings/frontend — restore the per-model compaction editor, the context-dial
  "fires at Xk" readout via an effective-behavior endpoint, and the collapsible
  status chip (the same surfaces ADR 0020 removed).

**Why this won't fail the same six ways (ADR-0020 → zcode mitigation map).**
This is the central decision; each historical failure has a direct countermeasure:

- *Trigger misses under per-model context settings* → trigger is computed from
  the **effective** window (`window − outputReserve − margin`) using exact
  measured `prompt_tokens`, and moves with the resolved model each turn.
- *Mid-turn 400s before any turn-start pass could see the growth* → the
  compaction pass runs at turn start against the persisted usage, and
  **micro-compaction** (idle/pressure triggered, no model call) provides an
  intermediate cheap layer so the full summary pass isn't the only safety net.
- *Summarizer cancellations and churn* → the summary call is a single
  non-streaming, tool-free request; a **circuit breaker** stops repeated
  attempts after a few consecutive failures instead of churning.
- *Cache-invalidation cost on every fold* → micro-compaction clears content
  cheaply and is the default first line; full model summaries are the less-
  frequent, higher-fidelity path.
- *Regressions in the cut/keep math* → keep budget is expressed as a fraction of
  the effective trigger/window with the invariant that an absolute trigger must
  be able to fold down to its own keep (the ADR 0004 #588 fault C), and the cut
  is always at a message boundary.
- *General robustness of the summary itself* → **graceful degradation** retries
  with progressive trimming and parses the provider token-gap, rather than
  failing on the first too-large summary.

**Summary prompt contract.** Tool-free plain text only, `<analysis>` then
`<summary>`, sections for intent / files-and-code / errors-and-fixes / pending
tasks / current work / next-step-with-verbatim-quote, and verbatim copy of
security-relevant instructions. (Adapted from zcode's `BASE_COMPACT_PROMPT`.)

**Immutability.** Compaction updates prompt-summary + watermark fields on the
conversation; it never deletes, replaces, or edits a message row. Transcript
review, export, and search keep reading originals. (Preserved from ADR 0004.)

**Legacy data.** Conversations compacted by the old ADR-0004 implementation
remain as-is; a surviving legacy system summary is used as prompt context when
recognized (as ADR 0004 specified).

## Testing Decisions

**What makes a good test.** Test external behavior at the prompt-construction
seam, not internal helpers: given a conversation whose measured `prompt_tokens`
exceeds the trigger, the prompt assembled for the next `model_client.chat` call
must (a) be within the model's effective context window, (b) contain the most
recent rounds verbatim, (c) contain a single bounded summary in place of the
older rounds, and (d) contain security-relevant instructions verbatim. Also test
that the original transcript rows are unchanged and that replay skips past the
watermark.

**Modules tested.**
- The assembled-prompt seam (highest, preferred) — drive the model-call loop with
  a mocked `model_client.chat` and assert on the prompt it received.
- The budget/policy unit (`context_window.py`) — effective window, trigger,
  circuit-breaker transitions, keep-budget invariant across absolute vs window
  triggers.
- The summary call — assert it is tool-free and non-streaming, and that a too-
  long summary request degrades (trims) rather than failing.
- `load_history` replay — assert the summary is injected and watermark-skipping
  behaves.

**Prior art.** The backend suite already covers `context_window.py` (the removed
feature's detection half survived) and the model-call loop; the compaction tests
should mirror that style. The prompt-seam tests are analogous to existing
prompt-manifest / prompt-construction tests: assert on the assembled prompt, not
on implementation internals.

## Out of Scope

- Re-implementing zcode's *other* high-value features (goal-mode verification,
  off-peak scheduling, the budgeted context builder, typed memory) — those are
  separate issues. This spec is limited to context detection + history
  compaction.
- Micro-compaction media handling (images/videos/files) is skipped, matching
  zcode (leave media verbatim).
- Any change to transcript retention/deletion, which ADR 0020 explicitly kept as
  a separate user-facing concern.
- New configuration schema beyond reusing the per-model compaction entry shape
  that ADR 0004 already defined.

## Further Notes

- The issue body already contains an agent's research analysis of zcode's engine
  (the "lessons to port"); this spec is the structured synthesis of that analysis
  against YAAH's actual current state (compaction removed, detection intact).
- Because ADR 0020 removed this feature after six rounds of pain, the highest-risk
  assumption is that the reintroduction regresses the same ways. The
  ADR-0020 → zcode mitigation map in Implementation Decisions is the spec's
  load-bearing claim and should be the first thing an implementer verifies.
- Consider a follow-up issue for the visible objective/iteration-card polish and
  any per-model trigger UI niceties, kept out of this spec's scope.
