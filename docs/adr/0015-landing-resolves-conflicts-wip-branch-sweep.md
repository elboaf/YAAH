# ADR 0015: Landing resolves conflicts and sweeps dirty-primary WIP to a wip branch

Date: 2026-10-08
Status: Accepted (amends ADR-0014's landing contract)
Driven by: maintainer decision in the ask-matt flow ("land (merge with
conflict resolution) should be a default option"); stranded-residue
case #337 (dirty primary touching the run branch's files froze the
landing, and only free text unblocked it)

## Context

ADR-0014 made "Land in the selected branch" an agent-executed option,
but its safe-sync SOP kept two walls: a **dirty primary tree**
(uncommitted human WIP — including WIP in the very files the landing
touches) froze the landing as manual-only, and a **merge conflict**
between the run branch and the target aborted the landing with a
rebase offer. Both walls reproduced the exact dead air #349 exists to
remove: the agent finishes, the landing refuses, and the user types
free text ("can you merge with conflict resolution please") to get
what they assumed was one of the options.

The two walls are independent:

- Conflict resolution happens entirely inside the chat's run worktree
  or in plumbing (`merge-tree`) — it never touches the human's tree.
- A dirty primary is the human's in-flight work. Any automated
  treatment of it must preserve it losslessly and visibly.

## Decision

1. **"Land in `<branch>`" is upgraded in place — it becomes the
   resolution-capable default.** No new ask option is added; the ask
   keeps its shape and option count. When the pre-flight finds a dirty
   primary or conflicts, the agent states the WIP fate (which files
   went to the wip branch, how to restore) in the option's description
   at ask time or in the landing report.

2. **Dirty-primary WIP is preserved on a local `wip/<slug>-<id>`
   branch, not stashed.** The sweep is **checkout-free** — the
   primary is never `git switch`ed (it is the human's checkout and
   must stay on `<branch>` so the post-landing `reset --hard` cannot
   move anything but `<branch>`): `git add -A`, then plumbing only —
   `git write-tree`, `git commit-tree <tree> -p <old target tip>`,
   `git update-ref refs/heads/<wip branch> <wip commit>`. The wip
   commit's parent is the target's pre-landing tip, so the WIP sits
   exactly where its author left it, one `cherry-pick <wip branch>`
   away from restoration — the restore command the landing report
   names. The sweep covers tracked changes AND untracked files, and
   leaves the primary clean for the landing. The wip branch is
   local-only, exactly like run branches (#320: never pushed, never a
   PR source), and is residue in the #290 sense: surfaced in the
   landing report, never silently removed. Stash was rejected: a
   stash pop that itself conflicts is a worse human-facing dead end
   than an inspectable branch.

3. **Merge conflicts resolve with the run-branch side winning**, on
   every landing path, and every resolution is listed file-by-file in
   the landing report. The run branch carries the built-and-tested
   work; the target usually diverged only by earlier landings. The
   plumbing form is
   `git merge-tree --write-tree --merge-base=<run base> <run branch> <target>`
   (replayed with `-X theirs` on conflict); positional
   base-as-branch1 would merge the wrong sides — the `--merge-base`
   form is load-bearing. The landing merge happens BEFORE the target
   ref moves, so the wip commit's parent and the merge's second
   parent are the same pre-landing tip; the merge commit (or
   fast-forward run tip) becomes the landing tip that the ref move
   and the working-tree reset then carry.

4. **Mid-merge states still freeze the landing.** A primary tree in an
   in-progress merge, rebase, or cherry-pick (MERGE_HEAD,
   CHERRY_PICK_HEAD, or rebase-merge/rebase-apply present) is a
   half-finished human operation, not ordinary WIP: unwinding it
   programmatically is the one treatment this ADR refuses. The
   landing is reported as manual-only, as under ADR-0014.

5. **The contract applies to all three selector-note landing paths**,
   each where its problem actually occurs: the master-pin/detached
   variant (safe-sync SOP, now preceded by the wip sweep), the normal
   chat-worktree variant (the primary is never touched there; its
   former "on conflict, `git merge --abort` and report" escape becomes
   resolve-with-run-branch-winning), and the degraded variant (run
   executed in the primary; same wip-branch treatment).
   "Land in another branch" inherits the same contract.

6. **Typed commands remain the fallback**, and run/wip branches stay
   local (#320). Nothing here authorizes a push or a PR from a run or
   wip branch.

## Consequences

- The GLOSSARY "Landing" entry drops "stop-and-report on conflict"
  and cites this ADR's resolution contract.
- ADR-0014 remains authoritative for the ask contract (option list,
  ask-once discipline, ask_user gating, sub-agent stripping) and for
  the safe-sync mechanics (update-ref before reset --hard); its
  dirty-primary freeze and conflict-abort outcomes are amended (its
  Status line records this), and its plumbing-merge example is
  corrected to the `--merge-base` form this ADR makes load-bearing.
- The wip sweep adds one new residue class: a local `wip/` branch the
  human must eventually restore or delete. It follows run-branch
  lifecycle visibility (surfaced in the residue protocol and the
  landing report, never silently removed).
- Prompt manifest regenerations that embed the selector note must be
  re-emitted so every surface carries the same contract.
