# YAAH

An agent harness where multiple chats and sub-agents can work on the
same workspace concurrently.

## Working tree discipline
Destination: ADR-0010 — per-chat worktrees. When the harness enforces
it, this section shrinks to project-specific notes and the run SOP
graduates into the base prompt. Until then:

If a run makes edits, it must not work in the primary worktree. Make a
run worktree and branch under `.scratch/`
(`git worktree add .scratch/run-YYYYMMDD-<slug> -b run-YYYYMMDD-<slug>`),
make and commit changes there, then land by merging onto the selected
branch and removing the worktree. Never switch branches in the primary
worktree — the selector is still a global checkout until #277 ships.
On landing conflicts, stop and report; leave the worktree and branch
for a human. Read-only runs skip all of this — no worktree, no branch.

Branch selector picks: if the conversation's system prompt includes a
"# Branch selector" note, the user has picked a branch for this chat, and
two rules above change for that run. Base the scratch worktree on the
picked branch:
`git worktree add .scratch/run-YYYYMMDD-<slug> -b run-YYYYMMDD-<slug> <selected-branch>`.
And landing targets the picked branch inside the scratch worktree, never
the primary worktree: commit on the run branch, then, in the worktree,
`git checkout <selected-branch> && git merge <run-branch> && git branch -d <run-branch>`;
from the primary worktree, `git worktree remove .scratch/run-YYYYMMDD-<slug>`.
If git refuses (the picked branch checked out elsewhere, or merge
conflicts), stop and report, leaving everything in place. Never check out
or move the primary worktree to honor a pick; merging a picked branch
into master happens only when the user explicitly asks.


## Agent skills

### Issue tracker

Issues and specs live as GitHub Issues on `elboaf/YAAH`, driven with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

See `docs/agents/triage-labels.md`. The five canonical labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. Exactly one state label per issue.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
