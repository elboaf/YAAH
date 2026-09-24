# Worktrees are session-scoped: one per chat, never released mid-session

Issue #58 gave every *writing turn* its own worktree and released it at
turn end. Reviewing the mid-run branch chip (47a623b) surfaced what that
model does to a multi-turn chat: every turn that touches a shell tool
mints a fresh worktree + `agent/*` branch from HEAD, and the turn-end
cleanup transforms uncommitted state (trash dropped, authored leftovers
salvaged to a patch, worktree removed). Observed 2026-09-22: two `pwd`
turns in one chat ran in two different worktrees on two different
branches; a file created uncommitted in turn 1 (a redirect capture) was
dropped as trash at turn end, so turn 2 of the *same chat* found the
file missing. Within-session variance between runs — the exact class of
surprise the isolation was meant to prevent, just one level up.

We decided: the worktree is bound to the CHAT SESSION, not the turn.
The first write-capable tool call creates it (lazy — read-only turns
never pay for isolation); it is never released mid-session. Every turn
still merges the session branch's new commits into the main tree under
the mutex (the user's folder must not lag) and fast-forwards the
session branch to the merged main HEAD, so the next turn starts from
exactly what the user sees. Uncommitted worktree state is deliberately
left in place across turns — turn N+1 works in exactly the tree turn N
left behind. The adr/0002 trash-drop / salvage / supervised-retry
machinery now runs once, at session end (`release_session`): chat
deletion, a workspace refile to a different repo, or the reaper for
orphans. A session branch whose commits are all merged (or that never
had any) is deleted at session end — no more per-turn branch litter.

## Consequences

- Within a chat, every run sees the same tree: stable `pwd`, turn N's
  uncommitted files visible to turn N+1, a refused merge stays
  retryable by the next turn (or by an explicit `git_merge_back` call)
  instead of stranding commits on a branch the next turn cannot see.
- The per-turn merge-retry nudge is gone: a turn may end with WIP in
  the worktree, like a human dev leaves a dirty checkout. The trash
  contract still applies at session end, so harness captures do not
  outlive the session as litter.
- A backend restart recovers the binding from the path shape
  (`.yaah/worktrees/<chat-id>` + an `agent/*` HEAD) on the next
  isolated call — the session's uncommitted state survives the restart.
- The chat-id dir name means one worktree per chat per repo; a chat
  refiled to a different repo mid-session releases (salvage-first) and
  re-creates.
- Agent-vs-user variance is NOT fixed by design: the worktree branches
  from HEAD, so the user's *uncommitted* main-tree edits stay invisible
  to the agent (issue decision 1: never stash user work), and the user
  editing main-tree files mid-session is not seen until a merge/ff
  catches up. Read-only shell commands still isolate (bash is a writer
  trigger); a read-only allowlist remains the lever for that, and
  composes with this ADR.
- Two chats on one repo see each other's work only after the other's
  turn-end merge (eventually consistent across chats, always consistent
  within a chat).

## Considered options

- **Create at chat creation (eager):** rejected — a chat does not exist
  until its first message (draft→adopt flow), so there is no clean
  earlier seam; it would mint worktrees for chats that never write, and
  the variance came from turn-end release/cleanup, not the creation
  moment. Lazy binding gives identical continuity.
- **Per-turn worktrees + read-only command allowlist (status quo +
  allowlist):** fixes the churn for recognized commands but keeps
  per-turn cleanup, so uncommitted state still cannot cross turns; the
  allowlist remains a composable future lever for WIP visibility.
- **Session binding (chosen):** strictly less machinery per turn (the
  probe/retry loop disappears), one new terminal path
  (`release_session`), and the accidental "merge timeout → next turn
  reuses the worktree" behavior becomes the designed path.

## Revision (2026-09-23): branch-first — turn end never merges

The per-turn merge-back was removed after a field incident: a chat
finished its work (merged, chip reverted to `master`, tree clean), the
user then said "push to github" — but the chat was still bound to its
session worktree, where HEAD is the `agent/*` branch. `git push` there
failed ("no upstream"), the model followed git's own suggestion and
published the AGENT branch to the remote with `--set-upstream` — while
`master` (the thing the user meant) stayed unpushed. The UI had claimed
`master` all along; the work actually lived on a branch the user was
never shown.

Decision revision: the harness does not merge into the main tree at
turn end. The session worktree and its `agent/*` branch are the work's
home until an explicit merge decision. This can be a direct request
("merge it") or be clearly implied by a user-requested continuation of
just-merged work, such as a release/version bump and push immediately
after the related work was merged. Ambiguous continuity is not enough;
ask before merging. Use `git_merge_back` and stop on dirty overlap or
conflict rather than improvising. The branch chip shows the agent branch
honestly, across turns, until the session releases. Turn end only
settles: a quiesced session (no commits, clean tree — the adr/0002
trash contract runs first, so a stray capture cannot pin it) is
DRAINED (worktree + binding + zero-commit branch removed); anything
with commits or authored dirt stays bound. The session-branch
fast-forward to main HEAD is kept (fast-forward only — a branch with
its own commits never moves). Unmerged `agent/*` branches are never
auto-pruned by the reaper: the branch is the record of the work; the
user deletes it.

For a clearly implied release push, verify the main workspace branch,
remote, and tree state, merge the follow-up, then push that primary
branch (never force-push). `git_push` in a session worktree publishes
the agent branch only; do not mistake it for pushing the primary branch.
If the push target is unclear, there are unexpected changes, or the
push is non-fast-forward, stop and ask. The invariant remains: the main
tree must not move as a side effect of turn end.