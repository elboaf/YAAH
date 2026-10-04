# Per-chat worktrees: each chat lands on its own selected branch

Date: 2026-10-03
Status: Accepted — supersedes ADR-0008 (worktree isolation removed),
firing the revisit condition 0008 wrote for itself. ADR-0003/0005/0007
remain superseded history.

## Context

ADR-0008 removed all worktree isolation because concurrent chats on one
repo were "rare to never", and wrote its own tripwire: if parallel
same-repo chats become a real workflow, the ADR must be revisited. They
became one — the #259 AGENTS.md trial made agents police shared-state
dangers with prompts, and issue #277 collected the design. This is the
revisit.

The prompt trial's wordiness is diagnostic: it compensates for shared
state. Meanwhile the harness's branch selector is a global `git
checkout` of the one shared checkout — one chat's switch yanks the tree
out from under every other agent. A worktree holds exactly one checkout,
so a per-chat branch is physically impossible without per-chat
worktrees.

The landing design was fought for the hard way, and every losing design
died the same death (issue #277 discussion, 2026-10-03): merging run
branches straight into master, a permanent `train` branch advanced by
`--ff-only`, and `switch -c` landing behind an ancestry guard — each was
secretly the same fix in a different costume: **serialize the writers**.
A shared landing tree is shared mutable state, and any mechanism that
lands into one must serialize its writers. What survives is the shape
that never lands into a shared tree at all.

## Decision

Each chat gets a private git worktree of the workspace repository. The
primary worktree — the workspace path as the human opened it — becomes
untouchable by agents by construction, not by prompt discipline.

- **Lifecycle — first write.** A chat's worktree materializes (at the
  issue's `.scratch/chat-<id>/` example path; chat ids are unique, so
  collisions cannot occur) at the first write of a run, checked out at
  the chat's selected branch. Not on chat creation, not on branch pick:
  idle and read-only chats cost nothing.
- **Branch selector — stored value.** The selector is a per-chat stored
  value on the conversation row: the chat's user-intended branch. On a
  chat with a worktree, flipping it runs `git checkout` inside that
  private worktree — safe because nobody else shares it. On a
  worktree-less chat it only records intent and touches nothing
  physical. The dangerous global semantic is gone.
- **Dirty-flip guard.** A selector flip refuses while the chat's
  worktree has uncommitted changes: commit or discard first. No
  stash-and-switch surprises.
- **Landing contract.** A run that edits works in a scratch worktree and
  branch inside the chat's worktree, commits there, then lands by plain
  merge onto the chat's selected branch — inside the chat's own
  worktree, where conflicts are that run's problem and `merge --abort`
  is free. Unresolvable conflicts stop-and-report; the primary worktree
  can never enter a merge state. The human integrates selector branches
  into master manually; the harness never moves master.
- **Selector tool.** The agent can create a branch and point the chat's
  selector at it on user request. Landing always targets the current
  selection; the selector is the single source of truth.
- **Endpoints.** The per-conversation git endpoints (`git-branch`,
  `git-info`, `git-branches`, `git-command`) serve the chat's own
  worktree; the draft/primary endpoints (`/api/workspaces/git-*`) stay
  the human's primary-worktree tools. The chip's per-chat meaning
  becomes truthful. The stale merge-mutex docstrings left behind by
  ADR-0008 go with this change.
- **Pruning — age, clean only.** A chat worktree with no commits and no
  uncommitted changes past an age threshold is auto-pruned. Chat
  deletion does not itself remove the tree; the sweeper is the single
  retirement mechanism, so uncommitted work always survives to the
  threshold.
- **Shell git stays.** No structured git tools return; agents work with
  shell git inside their worktree exactly as ADR-0008 decided. What
  returns is isolation, not ceremony.
- **Scheduled agents** are pinned conversations and inherit chat
  behavior until #278 (fixed / per-run / off landing modes) lands.
- **Remote workspaces stay out of scope v1** — no chat worktrees on
  `remote:<host>:<path>`; the gap is recorded here, consistent with the
  existing remote skips in AGENTS.md injection and the git endpoints.
- **Prompt graduation.** The AGENTS.md working-tree section shrinks to
  project-specific notes; the run SOP (scratch worktree, commit, land on
  the selected branch, clean up) graduates into the harness system
  prompt, reaching scheduled agents and remote workspaces where project
  AGENTS.md never travels (#259's endgame note on #277).

## Consequences

Accepted losses:

- **Disk.** One checkout per chat that has ever written, until pruned.
  Bounded by the clean-age sweeper; per-worktree untracked bulk
  (dependency installs and the like) is re-fetched by whichever tool
  creates it.
- **Flip friction.** A dirty chat must commit or discard before a branch
  flip. Accepted: silent stash-and-carry across branches is the failure
  mode this design exists to kill.
- **No auto-integration.** Work stops at selector branches; master only
  moves by human merge. Accepted deliberately — unattended integration
  into a shared tree is the serialization trap this ADR exists to avoid.
- **Remote chats keep today's exposure** until remote worktree parity is
  built.

Restored from ADR-0003's era, deliberately narrower: worktrees return,
but the placement classifier, merge mutex, dirty-overlap veto,
trash/salvage teardown, structured git tools, and merge-back engine do
not — the chat worktree is plain workspace management, and landing is a
plain merge in private space.
