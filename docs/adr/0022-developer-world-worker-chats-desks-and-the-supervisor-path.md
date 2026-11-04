# ADR 0022: The developer world — worker chats, desks, and the supervisor path

Date: 2026-11-04
Status: Accepted — supersedes ADR-0017 (the direct world) and, through it, the
isolation lineage it closed (ADR-0008, ADR-0010 and its five amendments,
ADR-0014/0015/0016). This is the brand-new decision argued on its own evidence,
made against a closed one: ADR-0017 deliberately wrote no revisit condition and
put the burden of a new decision on the proposer. The burden is discharged in
the Context below.
Driven by: maintainer design session 2026-11-04 (five-round grill interview),
the expiry of the concurrent-chats premise (below), and the overnight-agent
incident class (#619: an agent merged its own PRs and direct-pushed master).

## Context

ADR-0017 accepted concurrent-writer risk on a stated premise: concurrent chats
working the same repo are "rare to never", so isolation machinery is insurance
whose premiums exceed its claims. That premise has expired — multiple chats
running against one workspace is now an ordinary, daily workflow (parallel
chats, scheduled agents, spawned sub-agent fleets), and today they all share
one directory, one HEAD, one working tree. The accepted risk stopped being a
corner case and became the default operating condition.

At the same time, 0017's own diagnosis stands untouched: every incident of the
worktree/pin/landing eras was an incident of the machinery, not of the
underlying git. Four isolation regimes (0003/0005/0007, 0001/0002, 0010,
0014–0016) each grew ceremony to fight the last incident the ceremony created,
and each was deleted. The lesson this ADR builds on: **do not rebuild
machinery; change the premise.** Human developer collaboration solved this
decades ago, not with shared directories and harness ceremony, but with the
git workflow git was designed for — each developer works in their own clone,
on their own branch, and contributes through a review-and-merge path
(request-pull, now the pull request). Prior attempts failed because they tried
to make one directory behave like many. This ADR gives every agent what a
newly onboarded developer gets: their own directory.

## Decision

Two kinds of chat, and a contribution path between them:

1. **Ordinary chats keep the direct world.** Every chat YAAH has today is
   unchanged: full control of the workspace checkout. Any ordinary chat can
   act as **supervisor** — there is no designated or pinned supervisor role,
   and no artificial limitation between the user and their workspace.

2. **Worker chats** are a new, restricted chat type, spawned by an ordinary
   chat (first-class in the sidebar, steerable by the user or the supervising
   chat, independent lifespan). A worker is re-rooted by the harness at its
   **desk**: file tools, shells, and git endpoints resolve only inside the
   desk directory. This is enforced at the existing path-resolution
   chokepoint — strictly tighter than any current agent, but the same trust
   floor as the rest of the harness, not an OS-level jail. Workers cannot
   spawn workers.

3. **Desks** are seat directories outside the repo
   (`~/.yaah/desks/<workspace>/<n>`), each holding a clone of the workspace
   repo. One pool per workspace, created on demand, capped (default 4;
   spawn fails loudly at the cap). Assignment is a **handover**: fetch from
   the workspace repo and reset tracked files to the workspace's current
   state; untracked files (node_modules, builds) and old branches survive —
   the new developer inherits a used computer, just not at the same time.
   Desk directories outlive their worker chats; that inheritance is the
   point.

4. **Contribution.** The desk clone's only remote is the workspace repo; it
   has no origin remote and worker shells get no `gh` — the overnight-agent
   sin is structurally impossible from a desk. The only way work leaves a
   desk is **submit**: a request-pull-style contribution (summary, branch,
   base commit) into a per-workspace **inbox**, which wakes the supervising
   chat (starts its turn, or queues behind an active one). The supervisor
   fetches the branch straight from the desk directory, reviews, merges into
   the workspace checkout, and owns conflict resolution. Rejection **bounces
   to the authoring worker** with the review feedback; a contribution closes
   only on merge or explicit abandon. A worker holds at most one pending
   contribution; submit requires a clean desk. If the checkout is dirty or a
   root-capable turn is mid-flight, the supervisor defers — user WIP is
   never integrated through. If a supervising chat dies, the inbox persists;
   any ordinary chat can drain it.

5. **Origin.** When the workspace has an origin, the supervisor — never a
   worker — pushes branches, opens PRs, and shepherds CI; merging PRs on
   origin stays human-gated (standing rule since the overnight-agent
   incidents). **Origin is authoritative when it exists** (maintainer ruling,
   2026-11-04): the workspace checkout is the integration branch — supervisor
   merges land locally and are usable immediately, and after a PR lands
   (possibly squashed or reworked by review) the supervisor performs a
   deliberate **publish-and-reconcile**: reset local master to origin's
   merged state, guarded so reconciliation never discards locally-integrated
   work that has not landed on origin. Workspaces without origin skip this
   paragraph entirely.

6. **Scope.** v1 is the core loop — desks, worker chats, desk restriction,
   submit/inbox, wake-on-submit, supervisor merges — with minimal UI (worker
   badge, inbox surfaced inside the supervising chat, a soft warning when two
   root-capable chats work the same workspace at once). The origin hop and a
   dedicated inbox management page are v2. Non-git workspaces have no desks;
   remote workspaces are out of scope initially. Memory stays one store per
   workspace, keyed off the authoritative path so all desks share it;
   sub-agents inherit their chat's desk and restrictions; scheduled-agent
   chats are ordinary root-capable chats.

## Considered options

- **Pinned supervisor role / supervisor-only root access** — rejected: an
  artificial limitation between the user and their own workspace; any chat
  you drive keeps full control.
- **Rebuild harness isolation machinery (worktrees, pins, landing
  contracts)** — rejected: four deleted eras say the ceremony is the failure
  mode; this design wraps only ordinary git operations — clone, branch,
  fetch, merge — the workflow human collaboration already runs on.
- **GitHub-PR-centric transport** — rejected: couples the harness to a
  provider, excludes origin-less workspaces, and gives workers push rights
  to origin.
- **OS-level jails for workers** — rejected: out of proportion to the
  harness's existing trust model.

## Consequences

- `workspace_root`/`resolve_path` gains a per-chat re-root (worker → desk);
  `branch_select` becomes desk-local for workers automatically; per-chat git
  endpoints already funnel through `_conversation_git_root()`.
- The render matrix grows worker-conditional fragments; the prompt surface
  for ordinary chats must not change.
- Glossary gains the developer-world vocabulary (worker chat, desk,
  handover, contribution, inbox, submit, reconcile); "landing" remains a
  dead term.
- Desks live outside the repo: the ADR-0017-era residue cleanup
  (`legacy_residue.py`, #364) must never encounter or remove them.
- The direct-world risk is not gone — two ordinary chats in one checkout can
  still clobber each other. It is now accepted only for human-driven chats,
  with a soft warning; the structural cure for agents is spawning workers.
- ADR-0017's closure is honored in form as well as fact: nothing here was a
  "revisit" it invited; this document stands on new evidence — the premise
  it rested on expired.
