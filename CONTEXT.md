# YAAH

An agent harness where multiple chats and sub-agents can work in the
same workspace. The vocabulary below is about how their work lands in
the shared tree.

## Language

### Workspace

**Shared tree**:
Every chat and sub-agent works directly in the workspace's checkout.
There is no per-chat isolation: no session worktrees, no `agent/*`
session branches, no merge-back step. Work that should be durable is
committed with shell git, like a human's would be. See ADR-0008
(which supersedes ADR-0003/0005/0007).
_Avoid_: session worktree, main tree vs agent branch, git_merge_back

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

### Model scoping

**Model scope**:
What model (and routing provider) a chat, draft, or agent runs on: a stored value that must be COMPLETE (`provider::model`) or EMPTY (follow the global default). A complete scope is self-describing — the row names the provider that hosts its model, so later changes to the sidebar default cannot re-route it (#132).
_Avoid_: half-scope; "the model follows the sidebar"

**Bare id**:
A model id stored or sent WITHOUT its provider (`llama-3.3-70b`, not `groq::llama-3.3-70b`). Storage never writes this shape (qualify at write; boot repair fixes legacy rows); the resolver's bare branch is back-compat only and routes through the ambient active provider.
_Avoid_: storing bare ids; treating bare as a legal stored scope

**Default model** (sidebar):
The global default for NEW chats and for scopes that are empty — the active provider plus its remembered model. Changing it must never affect a chat whose scope is complete.
_Avoid_: "current model" (which chats read as their own scope)
