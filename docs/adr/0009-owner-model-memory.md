# Memory is a model of the owner, not a log of actions

YAAH injects a per-project memory index into every agent session, and the
prompt's save criteria decide what lands there. By 2026-10-02 the store for
this workspace held 86 files: 82 typed `project`, 4 `feedback` — mostly
issue/PR lifecycle notes ("filed", "return trip #2 pushed", "merged as
7c4b53ba") that the GitHub tracker already records, several events saved
twice under different slugs, and an index past its 12k-char truncation cap
so the newest entries reached no one.

We decided memory is **strictly a model of the owner**: `user` facts and
`feedback` on how to work (plus rarely a durable `reference` pointer).
Workflow state has exactly one home — the tracker; project knowledge has
exactly one home — the repo (CONTEXT.md, docs/, ADRs). Every save must pass
the **retention test**: would a fresh session, handed only the repo and the
tracker, work differently for this user without it?

The alternative — keeping project facts in memory whenever the repo doesn't
already record them — was rejected by the owner: unrecoverable-looking
project notes rot (stale issue links were found during the audit) and the
tracker/repo are the durable, reviewable homes. The existing store was
pruned accordingly (85 files → 4; backup: `~/.yaah/memory-backup-20261002.tar.gz`).

## Considered Options

- Owner-model first (chosen): memory = user + feedback; project facts only
  via repo docs, never memory.
- Broad charter with better dedupe: rejected — the invitation itself was the
  failure mode.
- Expire-on-close lifecycle notes: rejected — tracker already owns the data.

## Consequences

- Agents will occasionally lose genuinely-unrecoverable project facts; the
  answer is "write it into the repo or file an issue", not "save a memory".
- Index budget/reordering of the injected block is tracked separately
  (#192); this ADR governs only what may be saved.
