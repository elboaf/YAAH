# Merge-back is overlap-aware: git decides when dirt blocks a merge

Issue #58 originally refused every merge-back into a dirty main tree
(decision 1: "never stash user work"). The no-stash half is the real
invariant — stashing someone's WIP is the "silently rewrote reality"
class of bug #58 exists to kill — but the blanket dirty veto was
stricter than git itself: git only refuses a merge when uncommitted
files collide with files the merge updates, so YAAH blocked safe merges
whenever the human had any unrelated WIP (observed 2026-09-22: a
one-line backend fix could not land while unrelated `tools.py` edits
sat in the tree, though `git merge-tree` showed a clean merge).

We decided: merge-back checks the *overlap* between uncommitted
main-tree paths and the paths the branch changes (vs the merge base).
Overlap ⇒ refuse and name the files, never stash. No overlap ⇒ merge;
git's own atomic pre-flight remains the backstop if a colliding file is
dirtied mid-merge, and a successful merge around dirt notes it in the
result. Untracked files count as dirt only when the branch would
overwrite that same path.

## Considered options

- **Blanket dirty refusal (original decision 1):** most predictable,
  but turned every unrelated WIP into a hard blocker for multi-chat
  work — the friction scales with how much the human works alongside
  agents, which is the tool's whole premise.
- **Strict refusal + "merge anyway" override:** safest UX, but adds a
  confirmation step and a second code path; git's overlap rule already
  encodes the same judgment atomically. Revisit if git's rule ever
  surprises us in practice.

## Consequences

- Merge-back refusals now name the colliding files (`dirty_overlap` in
  the result) instead of reporting dirt generically.
- A git-level refusal ("local changes would be overwritten") is
  classified as a veto, not a content conflict (`conflict: false`).
- The running process picks this up only after a restart — until then
  end-of-turn merges still use the strict rule.
