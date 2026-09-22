# YAAH

An agent harness where multiple chats and sub-agents can work on the
same workspace concurrently. The vocabulary below is about how their
work is isolated and how it re-enters the shared tree.

## Language

### Isolation

**Agent worktree**:
A short-lived git worktree the harness gives a writing agent for the
duration of its run, on a scoped branch. The agent's tools are rebound
to it, so its changes never touch the shared tree mid-run.
_Avoid_: sandbox, isolated copy, side-branch

**Main tree**:
The workspace's primary checkout, shared by every chat and by the
human; the destination all work re-enters through.
_Avoid_: root workspace, master copy

**Shared-writer refusal**:
The error when a second writer tries to use a workspace that cannot be
isolated (not a git repo). Never a silent fallthrough to the shared
tree.

### Getting work back

**Merge-back**:
The end-of-turn handshake that merges an agent's scoped branch into the
main tree under the merge mutex. Refusals are surfaced, never papered
over.
_Avoid_: sync, check-in, promote

**Dirty overlap**:
Uncommitted main-tree files that a merge would overwrite. The only dirt
that vetoes a merge-back; unrelated WIP merges around.
_Avoid_: dirty tree (ambiguous — whose dirt, and does it collide?)

**Salvage**:
Capturing an agent's uncommitted worktree changes to a patch before its
worktree is destroyed, so a crashed or refused run loses nothing.
_Avoid_: backup, stash

**Merge mutex**:
The per-workspace lock that serializes every merge into the main tree —
agent merge-backs and UI git operations alike.
_Avoid_: git lock, merge lock file
