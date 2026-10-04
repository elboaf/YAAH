# YAAH

An agent harness where multiple chats and sub-agents can work on the
same workspace concurrently.

## Working tree discipline
Destination: ADR-0010 — per-chat worktrees. When the harness enforces
it, this section shrinks to project-specific notes and the run SOP
graduates into the base prompt. Until then:

If a run makes edits, it must not work in the primary worktree. Work in
this chat's run worktree at the deterministic path
`.scratch/chat-<id>/run` on branch `run/chat-<id>`
(`git worktree add .scratch/chat-<id>/run -b run/chat-<id>`; `<id>` is
this chat's conversation id — the run always knows its own name, no
discovery needed), commit there, then land by merging the run branch
onto the selected branch inside the run worktree, and remove the
worktree. Never switch branches in the primary worktree — the selector
is still a global checkout until #277 ships. On landing conflicts, stop
and report; leave the worktree and branch for a human. Read-only runs
skip all of this — no worktree, no branch.

Residue at run start (#290): if `.scratch/chat-<id>/run` already
exists, an earlier run left it. Clean AND fully merged into the
selected branch → landed-and-forgotten: remove it and proceed. Dirty
OR unmerged → untouched and surfaced: name it in your report and work
in `.scratch/chat-<id>/run-2` instead. The user says "land it" or
"scrap it". Residue is never silently deleted and never silently
blocks a chat.

Branch selector picks: if the conversation's system prompt includes a
"# Branch selector" note, the user has picked a branch for this chat,
and two rules above change for that run: create the run worktree based
on the picked branch
(`git worktree add .scratch/chat-<id>/run -b run/chat-<id> <selected-branch>`)
and land onto the picked branch inside the run worktree, never the
primary worktree: commit on the run branch, then, in the worktree,
`git checkout <selected-branch> && git merge run/chat-<id> && git branch -d run/chat-<id>`;
then `git worktree remove .scratch/chat-<id>/run`. If git refuses (the
picked branch checked out elsewhere, or merge conflicts), stop and
report, leaving everything in place. Never check out or move the primary
worktree to honor a pick; merging a picked branch into master happens
only when the user explicitly asks.


## Agent skills

### Issue tracker

Issues and specs live as GitHub Issues on `elboaf/YAAH`, driven with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

See `docs/agents/triage-labels.md`. The five canonical labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. Exactly one state label per issue.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
