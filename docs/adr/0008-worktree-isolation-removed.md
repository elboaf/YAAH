# Worktree isolation is removed: one shared tree, no isolation at all

Date: 2026-09-23
Status: Accepted — supersedes ADR-0003 (session-scoped worktrees), ADR-0005
(worktree seam operations), and ADR-0007 (invisible worktree isolation).
Parts of ADR-0001/0002 (merge-back refusal rules, write-provenance trash
classification) described machinery that only existed to serve isolation;
their guarantees are intentionally dropped with it.

## Context

ADR-0003 gave every chat a session-scoped git worktree on an `agent/*`
branch, with an explicit `git_merge_back` integration step, a merge
mutex, dirty-overlap veto, trash/salvage teardown, a reaper, background
sync, and UI (branch chip, pending-merge warnings, merge cards) to make
the machinery visible and honest. ADR-0007 tried to reduce the
model-facing ceremony. The full stack was ~1,400 lines of lifecycle
(`worktrees.py`), a placement classifier, ~500 lines of frontend, and
~3,100 lines of behavior-locking tests.

Reviewing the actual usage found the core premise unmet: **concurrent
chats working on the same repo are rare to never**, so the safety the
machinery buys is insurance that is never claimed — while its ceremony
(merge cards, `target=` parameters, prompt choreography, chip state) is
paid by every chat, every turn.

## Decision

There is no isolation. Every chat and sub-agent works directly in the
workspace's checkout (the main tree):

- `worktrees.py`, `wtclassify.py`, `provenance.py`, and `git_activity.py`
  are deleted. No bind/settle seam, no merge-back engine, no reaper, no
  background sync.
- All structured git tools (`git_status`, `git_diff`, `git_add`,
  `git_commit`, `git_push`, `git_pull`, `git_merge_back`) are removed
  from the model's tool list. The model uses shell git like any other
  command. There is no `target=current|main` duality — there is one tree.
- The frontend branch chip, pending-merge warnings, merge cards,
  worktree events, agent-branch-status probe, and git-activity
  telemetry are removed.
- `gitproc.py` (portable git discovery + spawn flags) survives: it is
  platform plumbing needed by `file_changes.py` regardless of isolation.

## Consequences

Accepted losses (each was weighed in review):

- **No merge veto.** Two writers on one repo can clobber each other's
  uncommitted edits live. Accepted: parallel same-repo chats are not a
  real workflow today; if they become one, this ADR must be revisited
  (git worktrees remain the known remedy — that is what ADR-0003 built).
- **No crash recovery.** A run dying mid-turn leaves partial state in
  the real checkout, exactly like a human's interrupted session.
  Accepted.
- **Trash/salvage classification is gone.** Harness captures and model
  writes are no longer distinguished at teardown. Accepted.
- CONTEXT.md's isolation vocabulary (Session worktree, Main tree,
  Integration, git_merge_back, shared-writer refusal) is removed.

The deletion test passes cleanly: removing the module removes ~2,300
lines of caller-side ceremony without moving complexity anywhere —
complexity that was defending against a scenario that does not occur.
