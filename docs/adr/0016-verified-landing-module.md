# ADR 0016: The landing SOP becomes a verified landing module (agent tool)

Date: 2026-10-08
Status: Accepted (amends ADR-0014's safe-sync SOP: verification is
mandatory and executed in code; amends ADR-0015: the wip sweep is
scoped to tracked + staged paths and the prose contract is retired)
Driven by: #353 (2026-10-07 primary-tree tangle), maintainer decision
in the ask-matt architecture-review flow

## Context

ADR-0014/0015 built the landing contract as **prompt prose**: the
selector note teaches the model the safe-sync SOP (~85 lines per
variant, with a second full copy of the same steps in the degraded
variant), and the model executes every leg by typing git commands into
the bash tool. The 2026-10-07 tangle (#353, forensics in #352) showed
what that trust is worth: two landings skipped the primary-sync leg,
one replaced master's lineage instead of merging, two raced from stale
bases, and no chat could cheaply query the primary's staged index to
tell a fossil from live WIP — nine chats to diagnose. Nothing in the
harness executes, verifies, locks, or even observes a landing; the
entire test surface is substring assertions on the note text.

Separately, the same incident exposed a lifecycle hole: post-run
worktree retirement sits unshielded in `run_agent`'s `finally`, where a
Stop-press cancellation kills the retirement's first git await
(`except Exception` does not catch `CancelledError`), and the sweep
backstop is once-per-boot, dead-chats-only, and 30-days-gated — so
landed-clean trees (chats 692–700) stood for a day.

## Decision

1. **Landing becomes code.** A verified landing module exposes one
   interface to agent chats as a harness-native tool (the sibling of
   the branch selector's tool): `land(target) → LandingReport`. The
   module executes the full contract in code, in order: freeze probe
   (mid-merge/rebase/cherry-pick still refuses as manual-only,
   ADR-0015 §4), checkout-free wip sweep, plumbing merge with the
   `--merge-base=` form and run-branch-wins resolution (ADR-0015 §3),
   update-ref, primary/tree reset --hard, and **post-verification**
   (HEAD == target and clean tree) — any violation fails the landing
   loudly with a structured `LandingReport`. The safe-sync mechanics
   are ADR-0014's, unchanged and now load-bearing in code: update-ref
   remains the only ref-move tool, and the reset leg can no longer be
   skipped silently because it is not a suggestion — it is the next
   line of the implementation.

2. **All three landing paths unify behind that one interface**
   (master-pin/detached safe-sync, normal chat-worktree merge,
   degraded run-in-primary), parameterized by target and root; remote
   chats ride the same module through the #333 gateway routing
   (`gitexec`). The degraded variant stops being a second prose copy
   of the SOP. The #278 per-run constraint (never merge into a shared
   branch on unattended fires) is enforced at the tool seam, not
   re-promised in prose. Sub-agents have no tool (the ask block is
   already stripped for them at the sub-agent prompt seam).

3. **Lineage freshness is checked immediately before every ref move,
   under an in-process landing lock.** The backend holds one asyncio
   lock; all agent landings flow through the one server process. Under
   the lock the module re-verifies
   `merge-base --is-ancestor <current target tip> <landing tip>`; if
   the target moved after the merge was computed, the merge is
   recomputed — the module replaces a lineage never, only extends it.
   A repo-level lock was rejected: the actual race was agent-vs-agent
   inside one process, and a lock file/ref adds a stale-lock lifecycle
   for no incident class the process lock misses. Out-of-band human
   landings remain possible and are fenced only by the freshness
   re-check — accepted residual risk.

4. **The wip sweep is scoped: tracked modifications + already-staged
   paths only; untracked noise is no longer snapshotted.** This amends
   ADR-0015 §2's `add -A` sweep, whose snapshot of untracked noise
   #353 flags as an open item. Loss analysis: untracked files are
   untouched by the post-landing `reset --hard`, so scoped sweeping
   loses nothing that `add -A` preserved — it stops *inventing* wip
   content (build output, stray scratch files) that the human then has
   to cherry-pick around. The wip branch carries exactly what the
   human was doing.

5. **Index provenance gets a one-command answer**: the same module
   exposes `fossil_probe(primary) → Verdict` — `git write-tree` vs the
   HEAD tree, differing blobs traced to refs (all-present ⇒ fossil
   candidate), returned as a structured verdict with evidence, usable
   by any chat in seconds. The next dirty-primary investigation takes
   one command, not nine handoffs (#353 acceptance criterion).

6. **The prose SOP is retired without a fallback.** Selector notes
   shrink to the #349 ask contract plus one instruction: execute the
   landing with the tool; if the tool reports failure, relay the
   manual-only report — never hand-run git against the primary. The
   fossil-forming path (a model typing update-ref/reset into the
   primary) is untaught, not merely unchosen. A compact prose fallback
   was rejected: it preserves the drift-prone duplication in
   miniature and keeps a taught path into the primary. The ask
   contract itself (options, ask-once discipline, typed "land it" /
   "scrap it" fallback) is ADR-0014's and is untouched; typed commands
   now instruct the tool rather than raw git.

7. **Post-run retirement is hardened in the same effort**: retirement
   runs under a bounded `asyncio.shield` (the pattern the sub-agent
   cleanup already uses), a retirement the window missed is enqueued
   for the sweep, and the sweep gains the hourly ticker its
   once-per-boot rate limiter already promised in comments. This
   closes both #353 lifecycle holes: cancellation killing retirement,
   and no retry path for a missed retirement.

## Consequences

- The 349/351 note-assertion test suites migrate: behavioral tests
  execute real landings in temp-repo fixtures (freshness-gate
  refusal, mid-merge freeze, scoped sweep round-trip, skipped-sync
  fail-loudly, re-merge on moved target); thin note tests pin only the
  ask contract. The degraded variant's reset leg — asserted nowhere
  today — becomes code with its own tests.
- Prompt manifests regenerate so every surface carries the shrunken
  note (ADR-0015's regeneration consequence applies here too).
- ADR-0014 remains authoritative for the ask contract; its SOP steps
  survive verbatim as the module's implementation order. ADR-0015
  remains authoritative for resolution semantics; its §2 sweep scope
  is amended as stated in its Status line here.
- Landing reports gain structure (machine-checkable), which the
  frontend residue badge and the ask-time WIP-fate description can
  render verbatim.
- Verified against git 2.53 behavior, as ADR-0014 was.
