# Invisible worktree isolation: the model never learns isolation exists

**Status:** Accepted (2026-10-13)

## Context

Agents kept mishandling git topology: wrong branch, premature
git_merge_back, confusion about whether work reached the main tree. The
root cause was not missing prompt rules — we already had the "Workspace
integration" note, the "Worktree lifecycle" note, target=current|main
parameters on four git tools, and the issue #123 escalation contract.
The root cause was that all of it is ceremony the model must execute
from memory over state it cannot see. Any invariant the model is
supposed to hold in its head is not an invariant — it is a suggestion,
and it degrades over a long context.

ADR 0003 gave every session a worktree and an `agent/*` branch; ADR 0005
collapsed the lifecycle into two operations (bind for write, settle
session). Both left the seam visible to the model: the model names
branches, picks merge targets, and follows merge choreography. That
visibility is the bug.

## Decision

Worktree isolation becomes invisible to the model. The model believes it
works on the branch the user selected, in the only tree that exists. The
harness owns branch creation, integration, pushing, and every git
topology decision, deterministically and testably.

**The model's git surface:**

- Git tools shrink to `git_status`, `git_diff`, `git_add`, `git_commit`
  with no `target` parameter — from the model's seat there is one tree.
- `git_merge_back` is removed from the model's tool list. Integration
  fires when the model signals task completion (explicit completion
  report — not turn end, not a user button).
- The model never initiates pushes. Push intent arrives through the
  user's chat and the harness resolves what to push (resolving cleanly
  to the branch the user selected) with existing safety rails (no
  force-push, clean-tree requirements).
- The prompt states nothing about branch identity — no names, no
  topology, no choreography. The "Workspace integration" and "Worktree
  lifecycle" prompt sections are deleted. Git-state questions the user
  asks in chat ("what branch am I on?", "push this") are answered by a
  single harness refinfo tool that translates and sanitizes; the model
  relays and never reasons about topology.

**What the harness owns:**

- Bind/settle, per ADR 0005 (unchanged).
- Output laundering: no `agent/*` strings or session-branch names reach
  the model through any tool result — including git_status output,
  merge results, and sub-agent spawn. Sub-agents get the same laundered
  world with no new spawn parameter; anything git-related they ask is
  answered by the same refinfo tool.
- Pre-merge commit check: at completion, if the session tree carries
  authored dirt, the harness asks once ("commit these files before
  integration?") — the commit-then-merge choreography leaves the prompt.
- Refusals as UX: the issue #123 dirty-overlap/conflict decision UI
  (commit / discard-and-merge / leave-isolated) is raised directly by
  the harness. The model never sees or handles a merge refusal; the
  escalation contract leaves the prompt.

**Scope:** universal default for every chat that passes a write gate —
no setting, no per-prompt-template opt-in. Scheduled-agent prompt
templates are rewritten to drop branch naming. The Windows sandbox and
remote runners follow the same contract; isolation is one seam (ADR
0005) and the mental model must be uniform wherever the model runs.

**Enabler:** the `worktrees.py` god-module is split by tenant
(wtclassify / provenance / gitproc + lifecycle) before the invisibility
work lands on it. Output laundering lives in gitproc; provenance absorbs
the tool-result observation interface. Splitting first means the
invisibility code lands on a clean seam instead of inside a 2127-line
file.

## Consequences

- The model's job reduces to: do the work, record it (commit), report
  done. Everything else is harness code.
- Traces that slip past laundering (a sha echoed in a log) are
  wrong-but-harmless: the prompt makes no identity claim to contradict.
- Users keep full git transparency through the UI and through
  refinfo-mediated answers; nothing about the user's view changes.
- Refines ADR 0003 (branch-first) and ADR 0005 (two-operation seam):
  unchanged in intent — this ADR removes the seam from the model's
  field of view, which ADR 0005 explicitly did not do.
- Delivery is one arc, ordered: (1) this ADR + CONTEXT.md entry, (2)
  worktrees.py tenant split, (3) prompt surgery + tool-surface shrink,
  (4) output laundering, (5) harness-owned merge at completion, (6)
  sandbox/remote parity, (7) sub-agent parity. Interim mixed states
  between tickets are acceptable; no stage ships the old ceremony back.
