# ADR 0017: The direct world closes the isolation question — runs execute in the workspace, permanently

Date: 2026-10-08
Status: Accepted — supersedes ADR-0010 (per-chat worktrees) and **all five of its
amendments** (selector pinning #312, degraded-mode landing #322, run branches
never reach origin #320, land-means-clean lifecycle #329, remote parity #305).
Supersedes ADR-0014 and ADR-0015 (the agent-executed landing contract and its
conflict-resolution / wip-sweep semantics) and amends ADR-0016 — its verified
landing module is deleted with the landing machinery it served, but the
decision that landed work must execute as verified code rather than prompt
prose is retained in spirit as a general design lesson.
Driven by: #359 (spec: worktree isolation removed for good), #360–#365
(delivered reality this ADR records), maintainer decision in the
architecture-review flow.

## Context

ADR-0010 reintroduced per-chat worktrees after ADR-0008 removed isolation,
firing 0008's own revisit condition: concurrent same-repo chats had become a
real workflow, and a per-chat branch is physically impossible without a
per-chat checkout. What followed was five amendments of landing machinery —
a stored branch pin with origin tracking, agent-executed landings, conflict
resolution with run-branch-wins, wip sweeps, a verified landing module, a
fossil probe, residue sweeping — each amendment fighting the last incident
the machinery itself created. The landing contract became the largest,
most-amended, most-tested subsystem in the harness, and every incident it
spawned (#337, #343, #352, #353) was an incident of the machinery, not of
the underlying git it wrapped.

The 2026-10-08 review weighed the whole stack against delivered reality:

- The per-chat checkout has real costs the isolation guarantees never
  reimbursed. Runs cannot see the user's live edits, the user cannot see the
  run's edits until landing, the branch chip needed its own state machine,
  and every prompt turn paid ceremony explaining where the work happens.
- The machinery's failure modes were worse than the risk it insured
  against. #353's nine-chat forensic chain was diagnosing landings —
  machinery artifacts — not concurrent-writer clobbering, which has never
  produced a single incident in the product's history.
- The 0008-era risk analysis still holds: concurrent chats writing the same
  files is rare, and when it happens the failure is exactly what a human
  collaborator's uncommitted edits produce — visible, local, and recoverable
  with ordinary git.

## Decision

The direct world: there is one tree — the workspace's checkout — and every
run, chat, and sub-agent works directly in it. ADR-0010's entire stack is
deleted: per-chat worktrees, run worktrees and run branches, the stored
branch pin (with its origin and staleness semantics), the end-of-work
landing ask, the landing module and its fossil probe, wip sweeps, and the
chip's per-chat state. The branch selector becomes `branch_select`: a plain
`git checkout` of the workspace on the user's request, guarded only by git's
own refusals, storing nothing (#361). One-time residue cleanup retires the
dead design's worktrees and merged run/wip branches at boot (#364), and the
dead pin/landing columns drop in place so upgraded databases match fresh
ones (#365).

**Concurrent-writer risk: accepted with eyes open.** Two writers on one
checkout can clobber each other's uncommitted edits. That risk is accepted
deliberately, on the 0008 evidence and the landing-machinery track record:
the insurance has never paid a claim, and its premiums (ceremony, state,
incidents) were compounding.

**No revisit condition.** This ADR deliberately writes none. The isolation
question is closed, not pending. ADR-0008's mistake was writing a tripwire
that fired it back into the design it had just deleted; this decision
records the closure instead. If a future maintainer believes isolation is
needed again, that is a brand-new decision argued on its own evidence,
made against a closed one — not a "revisit" this document invites.
Nothing here promises that evidence can never exist; it promises the
default is no, and the burden of a new decision is on the proposer.

## Consequences

- Prompt surface is minimal and ceremony-free: no run SOP, no branch note,
  no landing ask, no residue protocol (#360). The render-matrix manifests
  pin that shape; the drift guard holds it.
- Frontend: the chip is a plain workspace display; the branch/landing/residue
  feature UI is gone (#363).
- AGENTS.md and the agent docs carry no git SOP or working-tree section;
  the glossary speaks only direct-world vocabulary — "landing" is no longer
  a term in this project.
- The ~4,500 lines of landing/worktree/pin machinery and its behavior-lock
  tests are gone; complexity was deleted, not moved.
- Legacy-residue vocabulary lives only in history: ADR-0003/0005/0007/0008,
  ADR-0010/0014/0015/0016 (status lines point here), and the #364 cleanup
  module, which exists solely to erase its namesakes' residue.
