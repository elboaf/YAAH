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
human. Agent work reaches it through an explicit `git_merge_back` after
the requested task is complete; turn end itself never merges.
_Avoid_: root workspace, master copy

**Shared-writer refusal**:
The error when a second writer tries to use a workspace that cannot be
isolated (not a git repo). Never a silent fallthrough to the shared
tree.

### Conversation history

**Conversation transcript**:
The ordered, user-reviewable record of a conversation, including persisted messages and tool activity. It is preserved independently of model context.
_Avoid_: unqualified history when the transcript or model context is meant

**Model context**:
The system instructions and selected/replayed conversation content sent to the model for a particular call. It may be smaller than the transcript.
_Avoid_: transcript

**Prompt summary**:
A bounded, cumulative summary of transcript messages used in place of an older prefix in future model context. It does not replace or edit transcript messages.
_Avoid_: compacted history, replacement transcript

### Getting work back

**Integration**:
The agent's `agent/*` branch is an isolation detail, not the user's task
boundary. Complete ordinary requested code changes in the session worktree,
then integrate them into the main tree before reporting completion. Turn
end itself never merges. Do not ask the user to manage checkouts or merge
routine work. `git_push` in a session worktree publishes the agent branch,
not the primary branch. Never force-push.
_Avoid_: treating `git_push` as a primary-branch push; claiming isolated work is in main

**git_merge_back**:
The agent's explicit integration step for completed requested work. It
preserves unrelated user changes, but refuses overlapping uncommitted work
or conflicts. Never stash or overwrite user work. If integration fails,
classify dirty overlap, conflict, or other refusal; name affected paths;
confirm if the merge was aborted; and offer safe options with trade-offs
before asking how to proceed. Never claim unmerged work is in main.
_Avoid_: asking the user to merge routine changes; papering over conflicts

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

### Worktree operations

**Bind for write**:
The single operation a caller performs before its first write-capable
tool call: decide whether isolation applies, reuse or create the chat's
session worktree, and return a BindResult carrying the rebound
workspace path, the lifecycle event to record, and the model note for a
FRESH binding only (a reused binding emits nothing). Refusal rides on
IsolationRefused — the caller turns it into the tool's error result,
never a dead turn. Callers: the parent turn's gate and approval paths,
and the sub-agent runner.
_Avoid_: ensure_isolate-and-interpret, rebinding seam (that name stays
in the issue history, not the code)

**Settle session**:
The single operation a caller performs at turn end: drain a quiesced
session (no commits, clean tree) or report the surviving branch — as a
typed SettleResult whose status_note is the honest-status payload to
persist and emit (None when there is nothing to report). Turn end never
merges into the main tree; settling is not integration.
_Avoid_: turn-end cleanup, interpret turn_end dict
