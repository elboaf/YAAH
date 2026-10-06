# /implement-spec compatibility: convention-only support

Date: 2026-10-06
Status: Accepted — outcome (b) of issue #316's options (a–d).
Study-only issue: no production code changes; this ADR plus one skill
note are the deliverables.

## Context

mattpocock/skills v1.3 ships `/implement-spec`: an agent-loop spec
implementer built out of worktrees — an integration branch, a ticket
graph with a frontier, one implementer subagent per ticket in its own
worktree (built via /tdd), merger subagents landing finished tickets
onto the integration branch, then /code-review and a fixer. Issue #316
asked two questions: what ADR-0010's worktree implementation learns
from that approach, and what happens if a user runs the skill in YAAH
anyway.

Comparison, dimension by dimension (full table on the issue):

| Dimension | /implement-spec | YAAH (ADR-0010) |
|---|---|---|
| Unit of isolation | worktree per implementer, per ticket | worktree per chat; run worktree per run |
| Branch topology | one integration branch; per-implementer branches off it | chat-selected branch; `run/<slug>-<id>` per run; human integrates |
| Orchestration | the skill's agent loop | the harness: deterministic paths, injected run SOP |
| Landing | PR/merge to main per tracker | merge onto selected branch in the chat worktree; **master never moves** |
| Conflict handling | implementer merges integration tip first; merger resolves | that run's problem; `merge --abort` free; stop-and-report |
| Cleanup | skill step 9 removes implementer trees | `wt_sweep.py`: age + clean + dead-chat gates by path arithmetic |
| Concurrency | frontier parallelism in one loop | one run per chat; parallelism across chats |

Collisions if run unmodified (verified against the tree, #316 triage):

1. **Nested worktrees.** Implementer trees would sit under
   `.scratch/chat-<id>/`; `wt_sweep.py` finds chat trees by depth-one
   path arithmetic, and `git worktree remove` refuses on parents with
   registered children. Residue surfaces, never silently lost.
2. **Ticket-closing semantics.** Upstream closes tickets via PR/merge
   into main — a direct violation of the landing contract. The
   injected branch note outranks skills, but the skill speaks with a
   second voice.
3. **/resolving-merge-conflicts was deleted upstream** as "a harness
   concern, not a skill concern". YAAH *is* the harness, and its
   landing contract leans on conflict handling. Upstream explicitly
   blesses copying the archived skill into your repo. YAAH already
   vendors it (`backend/bundled_skills/resolving-merge-conflicts/`).

Candidate takeaways evaluated (from the issue):

- **Frontier scheduling** for scheduled agents (#278 fires
  one-ticket-per-run today): interesting, but a harness feature, not a
  skill — deferred, not decided here.
- **Integration branch as a selector concept**: compatible in spirit
  (a chat-owned integration branch is just another selector branch the
  human integrates once), but it adds a concept the landing contract
  does not need today; nothing forces it.
- **A subagent worktree primitive** ("spawn in a fresh worktree") is
  the real product feature hiding in this issue — recorded as a
  candidate, deliberately not built by this study issue.
- **Merger-as-review gate**: YAAH's analog moment is the landing
  merge; the convention below imports the ritual, not the machinery.

## Decision

**Outcome (b): convention-only support.** The skill stays unbundled
(re-affirming the issue's explicit out-of-scope), but a user may run it
by hand inside a chat, and it works — within a documented convention.

1. **Nested-worktree convention.** Implementer subagent worktrees are
   created UNDER the chat worktree, as siblings of the run worktree:
   `.scratch/chat-<id>/impl-<ticket>` — never deeper, never outside.
   This keeps them visible to the existing machinery:
   `wt_sweep.py`'s clean gate reads a whole `git status --porcelain`
   on the chat tree, so any surviving `impl-*` tree pins the chat tree
   dirty and nothing is swept out from under a live flow. Removal is
   **child-first**: remove `impl-*` worktrees (they are registered
   children — `git worktree remove` refuses a parent while children
   exist), then the run worktree, then the chat tree retires via the
   sweeper's normal gates. No sweeper code changes: the path
   arithmetic already sees `impl-*` as ordinary untracked entries.
2. **Landing-contract pin.** Pin line, to be stated verbatim wherever
   implement-spec-style flows are documented or run here:

   > **Landing-contract pin (ADR-0010):** landing requires a non-master
   > selection: if the chat's selector points at `master`, stop until
   > the selector points at a non-master branch. The integration branch
   > is the selected non-master branch (or a chat-owned integration
   > branch the selector points at); every landing is a plain merge
   > inside the chat's worktree; tickets close by landing, never by
   > PR/merge to master — **master never moves**.

   The harness-injected branch note outranks the skill's step 8
   ("resolve each ticket the way the issue tracker closes work");
   under this pin, "the way the tracker closes work" means land onto
   the selected non-master branch.
3. **/resolving-merge-conflicts stays vendored; deletion not
   accepted.** Upstream's rationale ("a harness concern, not a skill
   concern") argues *for* keeping it here: YAAH is the harness, its
   landing contract runs on conflict handling, and the skill is the
   documented procedure for that moment. The vendored copy is
   annotated (below) rather than re-derived.
4. **Native orchestration (outcome d) and adapted vendoring (c) are
   declined.** Bundling a patched skill forks it from upstream for one
   harness's semantics; building the frontier loop into the harness is
   a product decision with no demand signal yet. The frontier
   scheduling idea is recorded as a candidate for a future scheduled-
   agent integration mode, tracked on its own issue if wanted.

**Skill note (the vendored copy's annotation).**
`backend/bundled_skills/resolving-merge-conflicts/SKILL.md` carries a
compatibility note reconciling its "never `--abort`" rule with
ADR-0010's run SOP, where `merge --abort` is free: inside a chat's own
worktree a landing merge is disposable — abort freely and
stop-and-report; the never-abort discipline applies when finishing a
merge someone else depends on (the integration-branch role).

## Consequences

- No code changes: the convention rides on machinery that already
  works (sweeper clean gate, registered-children removal order, the
  injected run SOP). The convention lives here; if hand-run
  /implement-spec flows become common, graduating the child-first
  removal order into `wt_sweep.py` or a helper is a small follow-up.
- Residue from an abandoned convention run surfaces the same way run
  residue does today — visibly, at the chat dir — and retires via the
  normal sweep path once the children are removed.
- The subagent worktree primitive and frontier scheduling remain open
  product ideas; this ADR neither commits to nor forbids them.
