# YAAH

An agent harness where multiple chats and sub-agents can work on the
same workspace concurrently. The vocabulary below is about how their
work is isolated and how it re-enters the shared tree.

## Language

### Isolation

**Session worktree**:
A git worktree the harness gives a chat for its whole session, on an
`agent/*` branch named after the chat. The first write-capable tool
call creates it (lazy — read-only turns never pay for isolation), and
it persists across turns: turn N+1 works in exactly the tree turn N
left behind. A quiesced session (branch has no commits, tree is clean)
is drained at turn end; the next turn runs on the main tree and
re-isolates on demand.
_Avoid_: sandbox, isolated copy, side-branch, ephemeral worktree (it
is neither ephemeral nor per-run)

**Main tree**:
The workspace's primary checkout, shared by every chat and by the
human. Master moves only when the user merges deliberately — never as
a side effect of an agent turn ending.
_Avoid_: root workspace, master copy

**Shared-writer refusal**:
The error when a second writer tries to use a workspace that cannot be
isolated (not a git repo). Never a silent fallthrough to the shared
tree.

### Getting work back

**Branch-first**:
The contract: the agent's `agent/*` branch is the work's home. Turn
end never merges. Master moves only by explicit user decision —
`git_merge_back`, or plain git. Pushing publishes the agent branch,
which is correct, not a misfire.
_Avoid_: auto-merge, merge-back at turn end (the superseded contract)

**git_merge_back**:
The explicit merge of an `agent/*` branch into the main tree, invoked
when the user asks for it ("merge it"). Refusals are surfaced, never
papered over.
_Avoid_: sync, check-in, promote, auto-merge

**Dirty overlap**:
Uncommitted main-tree files that a merge would overwrite. The only
dirt that vetoes a git_merge_back; unrelated WIP merges around.
_Avoid_: dirty tree (ambiguous — whose dirt, and does it collide?)

**Salvage**:
Capturing an agent's uncommitted worktree changes to a patch before
its worktree is destroyed, so a crashed or released session loses
nothing authored. Provably machine-generated files (redirect captures,
log-shaped output) are dropped instead — with a patch of their own
when the classification is heuristic.
_Avoid_: backup, stash

**Drain**:
Session-end teardown of a session that carries no work: worktree
removed, zero-commit branch deleted, binding released. Sessions with
commits or authored dirt are never drained.
_Avoid_: cleanup, garbage collection

**Merge mutex**:
The per-workspace lock that serializes every merge into the main tree
— git_merge_back and UI git operations alike.
_Avoid_: git lock, merge lock file
