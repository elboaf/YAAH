# ADR 0014: Agent-executed landing into the selected branch — the master boundary becomes the safe-sync SOP

Date: 2026-10-07
Status: Accepted (amends ADR-0010's landing contract; landing
outcomes amended by ADR-0015 - dirty-primary WIP sweeps to a wip
branch and conflicts resolve with the run branch's side winning
instead of freezing/aborting)
Driven by: #349 (proactive end-of-work landing ask), maintainer decision

## Context

ADR-0010 set a hard boundary: the agent never moves `master`; the
human integrates selector branches into the primary branch. The
worst dead air in practice was exactly there — an agent finishes its
request, commits sit on the run branch, and the run ends waiting for
the user to remember the words "land it" or "scrap it". #349 inverts
the direction: where `ask_user` exists, the finishing agent asks
(Land in the selected branch / Land in another branch / Leave for
now / Scrap it) instead of waiting.

That inversion forces the question ADR-0010 sidestepped: when the
chat is pinned to `master` (the default inherited pin, checked out
in the primary worktree), "Land in `master`" is the option the user
most wants to pick. Refusing to offer it keeps the dead air this
issue exists to remove. The maintainer has decided the boundary is
reversed: agent-executed landing into the selected branch is
offered and performed, including when that branch is `master`.

## Decision

1. **The landing ask is a prompt contract.** The selector note
   carries an `# End-of-work landing ask` block instructing the agent
   to call `ask_user` once, when the user's request is complete and
   unlanded residue exists — never mid-task, never on a
   nothing-changed run, never re-asking an outcome the user
   pre-stated. No harness machinery forces the question.

2. **The ask block is injected only where `ask_user` exists:**
   interactive chats (no scheduled policy) always; scheduled runs
   only with the `allow_ask_user` opt-in (#93); unattended runs keep
   the passive residue protocol. Sub-agents receive the branch note
   with the ask block stripped (`_note_without_landing_ask` at the
   `_sub_agent_system_prompt` seam) — they have no `ask_user`.

3. **"Land in another branch" re-points the selector.** The user
   names branch Y; the agent calls `branch_select` to Y (the chat
   continues on Y), then lands into Y by the applicable SOP.

4. **Master landing follows the safe-sync SOP** (verified against
   git 2.53 behavior):
   - `git branch -f` refuses to move a branch that is checked out in
     any worktree; `git update-ref refs/heads/<branch>` does not —
     it is the only correct tool for the ref move.
   - Ordering is load-bearing: check the primary tree is clean
     (`git -C <primary> status --porcelain` empty) **before** the
     ref move; run `git -C <primary> reset --hard <branch>`
     **after** it (the working tree never follows a ref update on
     its own; a dirty tree must never be reset — so a dirty primary
     freezes the landing: ref unmoved, landing reported as
     manual-only).
   - A diverged run branch lands via a plumbing merge; since
     ADR-0015 the invocation is
     `git merge-tree --write-tree --merge-base=<run base> <run branch> <target>`
     (the `--merge-base` form is required — positional base-as-branch1
     would merge the wrong sides), conflicts resolve with the
     run branch's side winning, and the resolved tree is committed
     with `git commit-tree` before the ref move: **merge commits
     written by agents into the landing target are now allowed** —
     this expressly supersedes ADR-0010's assumption that master
     history stays linear and human-merged. (ADR-0015 removed the
     former abort-and-offer-a-rebase outcome on conflict.)

5. **Typed commands remain the fallback.** "Land it" / "scrap it"
   keep working in every surface that mentions them; the ask adds
   the proactive path, it does not replace the manual one. Run
   branches stay local (#320): nothing here authorizes a push or a
   PR from a run branch.

## Consequences

- The GLOSSARY "Landing" entry drops "the harness never does" as an
  absolute: agents land the chat's selected branch on the user's
  explicit ask-time choice, under the safe-sync SOP for
  checked-out-in-primary targets.
- ADR-0010 remains authoritative for worktree topology, the
  primary-tree checkout prohibition, pin semantics, and the
  #329/#320 lifecycle rules; only its landing contract is amended.
- The #278 per-run scheduler constraint ("never merge into a shared
  branch" in per-run mode) is unaffected: it governs unattended
  fires, which by definition have no ask-time user choice.
- Frontend residue-badge tooltips mention the ask as the primary
  path and the typed commands as the fallback.
