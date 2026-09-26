# The session-worktree seam lives in two operations: bind for write, settle session

**Status:** Accepted (2026-09-26)

## Context

The worktree module (`backend/agent/worktrees.py`) grew a rich primitive
surface — `ensure_isolated`, `turn_end`, `binding_for`, `worktree_of`,
`release_session`, git/mutex helpers — and every caller reassembled
lifecycle semantics from those primitives. Concretely, `loop.py` carried
the same ~55-line block in both its ungated and its approval gate paths:
call `ensure_isolated`, decide whether the rebind was fresh by comparing
paths, re-read `binding_for` for branch info, emit `worktree_bound`,
snapshot the new workspace as the change baseline, and append a model
note. The sub-agent runner carried a fourth copy of the pattern. At turn
end, the loop interpreted `turn_end()`'s raw status dict into git
activity, persistence, and UI events. A change to drain or bind
semantics touched four places.

## Decision

Callers use exactly two operations:

- **`bind_for_write(workspace, chat_id)`** → `BindResult` (typed): the
  rebound workspace, lifecycle event, branch facts, and the model note
  for a FRESH binding (empty when reused). Refusals raise
  `IsolationRefused` — surfaced as the tool's error result, never a dead
  turn.
- **`settle_session(chat_id)`** → `SettleResult` (typed): drained flag,
  surviving-branch facts, and a pre-built `status_note` (None when
  nothing to report). Turn end never merges; settling is not
  integration.

The loop and the sub-agent runner stay emitters: they yield events and
record git activity from the results, but they no longer reconstruct
lifecycle semantics. The primitives (`ensure_isolated`, `turn_end`,
`release_session`, trash/salvage classification, the merge mutex) become
implementation details of the two operations.

## Consequences

- Duplicated gate/approval bind blocks collapse to one call shape; drain
  and salvage rules land in one place.
- New callers (scheduler fires, future turn engines) cross one seam
  instead of learning the primitives.
- New tests assert on `BindResult`/`SettleResult` directly, without
  driving the whole turn loop.
- Does not change behavior: `test_agent.py` and `test_worktrees.py`
  lifecycle tests pass unmodified (behavior-locked refactor), plus new
  seam-level tests in `backend/tests/test_worktree_seam.py`.
- Refines adr/0003 (branch-first): unchanged in intent — this ADR
  records *where the seam lives*, not new merge policy.
