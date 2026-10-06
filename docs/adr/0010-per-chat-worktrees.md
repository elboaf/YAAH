# Per-chat worktrees: each chat lands on its own selected branch

Date: 2026-10-03
Status: Accepted — supersedes ADR-0008 (worktree isolation removed),
firing the revisit condition 0008 wrote for itself. ADR-0003/0005/0007
remain superseded history.
Amended 2026-10-04: selector pinning semantics (pin at creation,
primary-tree immunity, switch-request translation, sub-agent
injection) — see the amendment section below.
Amended 2026-10-05: universal degraded-mode landing rule (#322) —
see the amendment section below.

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
  _Amended 2026-10-04: the tool is the only documented path, and the
  start point is server-enforced as the selector's own branch at call
  time — the primary worktree's HEAD is never consulted._
- **Endpoints.** The per-conversation git endpoints (`git-branch`,
  `git-info`, `git-branches`, `git-command`) serve the chat's own
  worktree; the draft/primary endpoints (`/api/workspaces/git-*`) stayed
  the human's primary-worktree tools at this amendment's writing. The chip's per-chat meaning
  becomes truthful. The stale merge-mutex docstrings left behind by
  ADR-0008 go with this change.
  _Amended 2026-10-04: the draft destination card no longer performs a
  primary-tree checkout — its branch pick pre-stores the new chat's
  `selected_branch` at creation. No in-YAAH control moves the primary
  worktree; the human switches it via terminal git. Amended again for
  #301: `/api/workspaces/git-checkout` is removed entirely (the
  remaining `/api/workspaces/git-*` endpoints are read-only), and every
  local-git-workspace chat is pinned at creation — the no-pick path
  inherits the workspace's branch (`branch_pin_origin='inherited'`),
  legacy NULL rows lazily pin on first read._
- **Pruning — age, clean only.** (Superseded 2026-10-06 by the
  land-means-clean amendment below, #329.) A chat worktree with no commits and no
  uncommitted changes past an age threshold is auto-pruned. Chat
  deletion does not itself remove the tree; the sweeper is the single
  retirement mechanism, so uncommitted work always survives to the
  threshold.
- **Shell git stays.** No structured git tools return; agents work with
  shell git inside their worktree exactly as ADR-0008 decided. What
  returns is isolation, not ceremony.
- **Scheduled agents** carry their own landing setting (#278): off (the
  default — inherit chat behavior), fixed (every fire lands on one
  named branch), per-run (each fire gets a collision-bumped
  `<agent>-<date>-<time>` branch, left unmerged for manual
  integration). The mode is written through to the chat's pin at fire
  time; landing itself stays prompt-driven via the #277 selector note,
  and the harness never merges into shared trees unattended.
- **Remote workspaces stay out of scope v1** — no chat worktrees on
  `remote:<host>:<path>`; the gap is recorded here, consistent with the
  existing remote skips in AGENTS.md injection and the git endpoints.
- **Prompt graduation.** The AGENTS.md working-tree section shrinks to
  project-specific notes; the run SOP (scratch worktree, commit, land on
  the selected branch, clean up) graduates into the harness system
  prompt, reaching scheduled agents and remote workspaces where project
  AGENTS.md never travels (#259's endgame note on #277).

## Amendment 2026-10-04: selector pinning semantics

Grilling session on the selector's relationship to the primary
worktree. Five decisions, all subordinate to this ADR; the chip's
tri-state and migration details are slice-level and live in the issue
tracker, not here.

1. **Pin at creation.** Every chat in a git workspace records its
   branch at creation: from the draft destination card's pick if one
   was made, else from the workspace's then-current branch. The chip
   distinguishes inherited pin from explicit pick (e.g. a
   "· workspace" marker) and flags a stale pin whose branch no longer
   exists. Non-git workspaces stay as today: no chip, no pin.
2. **Primary-tree immunity.** An external `git switch` on the primary
   worktree — terminal or otherwise — affects no chat's aimed branch,
   picked or inherited. Sequencing note: until the worktree
   materializer lands, this immunity is semantic (what scratch
   worktrees base on and what the prompt note states), not yet
   physical; chip dirty/ahead-behind reads still follow the primary
   tree until per-chat worktrees exist.
3. **Switch-request translation.** A user request to switch branches —
   including "put it back on main" — is a selector change and nothing
   more. No agent ever runs a raw `git switch`/`checkout` on the
   primary worktree; the human drives the primary tree via terminal
   git. Enforcement is prompt-only for now (AGENTS.md working-tree
   section plus the injected selector note); a git-shim guard was
   considered and deferred — revisit only if prompt-only violations
   are observed.
4. **Start point.** A user request for a new branch derives it from
   the chat's selected branch, never the primary worktree's HEAD.
   Delivered by the amended selector tool (server-enforced start
   point, atomic create + point); shell git stays possible but is not
   the documented path.
5. **Sub-agent injection.** The harness appends the same branch note
   it gives chat turns to every spawned sub-agent prompt, so
   delegation cannot silently drop branch context.

## Consequences

Accepted losses:

- **Disk.** (Amended 2026-10-06, #329: clean trees retire at run end.)
  One checkout per chat that has ever written, until pruned.
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

## Amendment 2026-10-05: run branches named after the work (#312)

Run branches rename from `run/chat-<id>` to `run/<title-slug>-<chat-id>`
(e.g. `run/fix-tts-whine-631`) so `git branch` reads like a changelog.
Decisions:

- **The chat id stays in the name.** Titles repeat and change mid-chat;
  the id suffix keeps names collision-free and gives a stable
  cross-reference (`git branch --list '*-631'`) across renames.
- **Generic titles fall back.** While the title is still the mechanical
  default ("New Task") or slugs to nothing, the name stays
  `run/chat-<id>` — no fake descriptiveness.
- **Presentational only.** The name remains prompt discipline, not
  enforcement, and every programmatic consumer (the #290 run badge,
  the sweep, residue classification) keys off the deterministic
  `.scratch/chat-<id>/` PATH, never the name. A mistyped slug is
  cosmetic. The path namespace is untouched, so residue from before
  this amendment surfaces exactly as before.
- **Slug rule.** Lowercase, `[^a-z0-9]+ → -`, trimmed, capped at 40
  chars. Unicode titles slug to their ASCII remainder or fall back.

Pre-existing `run/chat-*` branches need no migration: they land, sweep
and surface through the same name-blind machinery.

## Amendment 2026-10-05: universal degraded-mode landing rule (#322)

When a chat worktree cannot be materialized (non-repo, git failure), the
degraded run SOP's landing rule previously deferred to a per-workspace
"AGENTS.md interim master-landing rule" — a rule that existed in no
workspace (a dangling leftover from the pre-#277 SOP). Decisions:

- **The SOP ships universal in the harness prompt.** No note variant may
  reference workspace-specific rules or files; a workspace AGENTS.md
  contributes only project-specific deltas on top. YAAH's own AGENTS.md
  is pinned (test) to NOT restate the run SOP — the injected note is
  the single source, everywhere.
- **Degraded landing: land when safe, else commit-and-report.** Safe =
  the selected branch is checked out in no other worktree, discovered by
  the run itself via `git worktree list` (prose, not harness code —
  zero git calls added to the path where git is already failing).
  Merging onto an unchecked-out branch moves only the ref; no human's
  working tree is touched. When the branch IS checked out elsewhere
  (the common degraded case), the run commits in its run worktree,
  removes it, and reports — the human lands.
- **The residue protocol is extracted** into a shared helper
  (`_residue_protocol`) so every selector-note variant states it
  identically.

Work still stops at selector branches in the normal path; this amendment
only repairs the degraded fallback so no workspace ever improvises a
landing rule again.

## Amendment 2026-10-06: run branches never reach origin (#320)

Run worktree branches (`run/*`, named per the #312 amendment) are
private scratch: they exist only in the local repository and reach
origin solely as commits inside a landed primary branch, which the
human pushes. Decisions:

- **Landing is local-merge only.** No PR-based landing: opening a PR
  from a run branch requires publishing that branch as an origin ref,
  which is exactly the leak this amendment closes (observed on #318 and
  again on #325-#327). The #322 degraded probe (`git worktree list`)
  already keeps landing off checked-out branches; it stays the safe
  path.
- **The run SOP forbids publishing.** Every run-branch note variant
  states the rule - never pushed, never PR'd (`_LOCAL_ONLY_RULES` in
  loop.py); an agent inside a run worktree cannot rationally publish.
- **The status strip refuses run-branch pushes.** The UI git-command
  push action - and its `--set-upstream` fallback, which minted
  brand-new origin refs for upstream-less branches - refuses when HEAD
  names a `run/*` branch; the refusal is traced like any other result.
  Agents get the same rule through the SOP; there is deliberately no
  second server-side chokepoint shadowing arbitrary agent git execs.
- **Landed run refs are cleaned up.** When a landed PR's head turns out
  to be a run branch, deleting the origin ref after the merge is part
  of landing; the local run branch is already deleted by the SOP's
  teardown.


## Amendment 2026-10-06: land-means-clean (#329)

A worktree exists only while its work is in flight. In the requester's
words, confirmed in-session: "A worktree exists only while its work is
in flight. When the work lands on the chat's selected branch, everything
created for that work goes away - run worktree and the chat worktree if
the chat has nothing left in it. Only dirty residue survives landing,
and then it's surfaced to the user, never silently kept. Branches are
never held open by finished work." Decisions:

- **Post-run retirement.** When a run ends and the chat worktree is
  clean and residue-free, the harness retires it, freeing any branch
  checkout the tree held. The agent never removes the tree it stands
  in: this is harness code after run end, never agent SOP.
- **The sweeper's live gate is per chat, not per workspace.** A dead
  chat's clean tree sweeps even in a workspace with live conversations.
  (The per-workspace gate made the sweeper a permanent no-op in the
  main workspace - the pile-up #329 documents.)
- **Pre-#277 standalone clones** (`.git` a directory) become retirably
  visible: clean-checked and removed like any other residue-free tree.
- **Dirty residue survives and surfaces** via the residue protocol -
  never silently kept, never silently deleted.
- **Re-materialization is cheap.** `ensure_chat_worktree` recreates the
  tree on the next run; #321 keeps the namespace git-invisible.
- **The age threshold stays as backstop** for residue the run-end path
  missed (a crashed run), not as the primary lifecycle.
