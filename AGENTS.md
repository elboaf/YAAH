# YAAH

An agent harness where multiple chats and sub-agents can work on the
same workspace concurrently.

## Working tree discipline

The run SOP — worktree paths, branch grammar, landing, residue —
is not this file's business: YAAH injects it into every run's system
prompt with the chat's own ids filled in (ADR-0010, #277/#322; the
design and its amendments live in `docs/adr/0010-per-chat-worktrees.md`,
vocabulary in `CONTEXT.md`). External agents without that injection:
never move or check out the primary worktree, and leave any worktree
under `.scratch/` alone — surface it, don't delete it.


## Agent skills

### Issue tracker

Issues and specs live as GitHub Issues on `elboaf/YAAH`, driven with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

See `docs/agents/triage-labels.md`. The five canonical labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. Exactly one state label per issue.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
