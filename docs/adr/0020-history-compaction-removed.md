# ADR 0020: History compaction removed

Date: 2026-10-31
Status: Accepted (supersedes ADR 0004)
Driven by: #620, maintainer decision

## Context

ADR 0004 shipped history compaction: when a conversation's measured
`prompt_tokens` crossed a trigger (absolute or fraction-of-window), the
oldest transcript prefix was summarized by an auxiliary model call and
replayed as a single injected summary message instead of the raw rows.

The feature failed across six rounds of fixes: trigger misses under
per-model context settings (#588 class), mid-turn 400s before any
turn-start pass could see the growth, summarizer cancellations and
churn (#389), cache-invalidation cost on every fold, and repeated
regressions in the cut/keep math. Owner call: remove the feature
entirely rather than attempt a seventh repair.

## Decision

Compaction is removed completely:

- `backend/agent/compaction.py` and `backend/agent/model_settings.py`
  are deleted. `split_model_route` moved to
  `backend/agent/context_window.py` (it was that module's only shared
  export).
- The trigger watch, turn-start pass, mid-turn re-check, summarizer,
  `compacted`/`compaction_failed` events, and the
  `/api/compaction/effective` endpoint are gone.
- Config keys `compaction`, `model_compaction` (and the per-model
  trigger fields in Settings) are gone; per-model max steps (#111)
  remain.
- DB columns `prompt_summary`/`prompt_summary_through_message_id` are
  no longer created or read; existing columns in already-initialized
  databases are simply ignored.
- The frontend loses the compaction chip, the context-dial trigger
  tick, and the per-model compaction editor; the dial keeps the
  120k dumb-zone color band.
- The prompt-manifest matrix drops the `-compaction` axis (151 -> 85
  combos) and its committed manifests are regenerated.

## Consequences

Long sessions hit the model's context ceiling unguarded; the user
starts a new chat (the context dial still shows growth). Transcript
replay is now strictly append-only: full history on every turn, image
pruning and tool-result clipping unchanged. `load_history` lost its
watermark parameter — it always returns the full transcript.
