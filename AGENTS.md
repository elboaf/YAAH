# YAAH

An agent harness where multiple chats and sub-agents can work on the
same workspace concurrently.

## Working tree discipline
If a run makes edits, it must not work in the primary worktree. Instead:
1. First make a new branch and a new worktree under `.scratch/` to act as
   your workspace for this run, e.g.
   `git worktree add .scratch/run-YYYYMMDD-<slug> -b run-YYYYMMDD-<slug>`
   (the date+slug keeps concurrent sessions from colliding).
2. Make all of this run's changes there.
3. When done, stage and commit on the worktree branch, merge the branch
   back into the primary worktree, then remove the worktree:
   `git worktree remove .scratch/run-YYYYMMDD-<slug>`.
Read-only runs (research, review, exploration) skip this entirely — no
worktree, no branch. If merge-back conflicts, stop and report; leave the
worktree and branch in place for a human.

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
