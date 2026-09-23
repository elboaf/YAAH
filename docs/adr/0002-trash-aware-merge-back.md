# Merge-back drops harness-generated trash; a refusal is a task for the agent, not a chat status

> **Status:** the merge primitives described here (merge_back /
> git_merge_back, dirty-overlap refusal, trash contract) are still
> current — but their AUTOMATIC per-turn invocation is gone. Since the
> adr/0003 revision (2026-09-23, branch-first) master moves only on an
> explicit user-requested git_merge_back; turn end never merges.


Observed 2026-09-22, shipping v1.0.9-rc.8: an agent merged PR #96 and
bumped the version in its worktree, committed everything, and reported
success. At end of turn, merge-back **refused** — the worktree held one
uncommitted file, `tsc-out2.txt`, a failed-typecheck capture the agent
had left behind via a shell redirect. The salvage patch (meant to
protect real work) faithfully preserved the one junk file. The main
tree stayed three commits behind the released work, and nothing in the
product pulled the refusal back into the conversation — the red pill
was rendered, but neither the agent nor the user acted on it. The user
discovered the drift by asking why their folder didn't have the
release, and was told to run `git pull` by hand — the exact
human-in-the-loop step the #58 design exists to eliminate.

Two root causes, two decisions:

1. **The salvage path cannot tell work from garbage.** Any uncommitted
   byte is treated as possibly-precious, so a log vetoes a merge. We
   now classify worktree dirt at merge time; provably
   harness-generated files are dropped, everything else keeps the old
   refuse+salvage path. Classification is provenance-first (the
   harness records what its tools write: `write_file`/`create_file`/
   `edit_file` payloads are `model`; shell-redirect captures are
   `tool`), with conservative structural fingerprints second (log/
   temp/patch extensions, diff walls, base64 density, balanced JSON
   depth, log-shaped tails). The dangerous direction is a false TRASH,
   so uncertainty always classifies as WORK: an unknown text file is
   salvaged exactly as before this change.

2. **A refused merge-back must not end the turn as a status the user
   has to interpret.** The loop now probes the worktree before the
   final answer completes: if authored dirt remains (and the agent can
   fix it — it owns the worktree), the turn continues with up to two
   supervised cleanup rounds: a system nudge names the files, the next
   model call has full tool access, and the probe repeats. The probe
   never merges — the `finally` block still owns the real merge-back,
   so the success pill is emitted exactly where it always was. A
   main-tree refusal (the user's dirty overlap, a conflict) is *not*
   surfaced to the model — nudging the model cannot fix the user's
   tree; that path renders the red pill as before.

Third, the visible folder gets a safety net: a background task
fast-forwards every registered workspace that sits strictly behind its
upstream (ff-only, merge-mutex-serialized, git's own overlap-aware
working-tree guard). A refused or crashed merge-back can no longer
leave the user's folder silently behind the work that shipped: the
folder catches up on the next sync tick without touching diverged
history or uncommitted files.

## Considered options

- **Provenance-only classification** (no content fingerprints): clean
  theory, but the incident's own file (`tsc-out2.txt`, written via
  `node tsc ... > file`) would still slip through on any code path
  that misses the redirect parse — fingerprints are the backstop that
  makes the guarantee hold.
- **Fingerprints-only** (no provenance): simpler, but a false TRASH
  would rest on heuristics alone with no recorded ground truth to
  check against; provenance keeps the harness honest about what it
  itself wrote.
- **Auto-commit leftover dirt before merging**: rejected — it launders
  junk into history and can commit half-authored work the model never
  meant to land. Deletion is only for files the harness itself wrote;
  salvage remains the path for everything uncertain.
- **Retry loop without a limit**: rejected — a model that cannot clean
  its worktree would burn steps forever; two supervised rounds then
  the honest refusal.
- **User-facing "sync now" button for the folder sync**: rejected for
  now — the user should never need to know the folder drifted; if the
  sync ever refuses repeatedly (diverged tree), that deserves a
  surface, but as an anomaly report, not a maintenance chore.

## Consequences

- A dirty worktree no longer reliably blocks merge-back: generated
  captures, logs, and temp artifacts are dropped at merge time
  (`dropped_trash` in the result notes what was removed).
- `self_merge` gained a `final` flag: `final=False` (the loop's probe)
  drops trash and reports `retry_dirty` without merging or unbinding;
  `final=True` keeps the terminal salvage contract. The chat binding
  survives a probe so the retry works on the same worktree.
- Sub-agent finalization uses the same trash contract before salvage.
- Turns can now take up to two extra model steps for merge-back
  cleanup; the nudge is a system message and never renders as the
  agent's voice.
- The backend runs a 15-minute main-tree sync (20s after startup);
  ff-only means a diverged workspace is skipped, never reset.
- The running process picks all of this up only after a restart.
