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

### Prompt surface

**Prompt surface**:
Everything YAAH can send to the model as instructions or tool descriptions, across every runtime configuration. The complete object a prompt review must walk.
_Avoid_: "the system prompt" (names only the base), prompt text

**Base prompt**:
The instructions sent with every chat turn before conditional additions. An override replaces it wholesale — it never composes with one.
_Avoid_: default prompt; system prompt (when the base is meant)

**Conditional fragment**:
A section of the prompt included only when its trigger condition holds at assembly time.
_Avoid_: dynamic prompt, optional section

**Injected content**:
Model-context text authored outside the harness at runtime — project notes, the memory index, skill bodies — that the prompt surface carries but does not author.
_Avoid_: dynamic content; attachments (those ride messages, not the prompt)

**Auxiliary prompt**:
A one-off prompt for a model call that is not a chat turn, such as compaction summaries or title generation.
_Avoid_: side prompt, internal prompt

**Sub-agent prompt**:
The system prompt assembled for a spawned sub-agent, composed separately from the chat base prompt.
_Avoid_: nested prompt

**Render matrix**:
The complete set of prompts the harness can assemble, enumerated across every fragment's trigger conditions; produced by the manifest harness for review and drift checks.
_Avoid_: prompt snapshots (implies byte-goldens)

### Memory

**Memory**:
The per-project store of durable facts about the owner — user facts and working feedback — injected into every session's prompt. It models the owner, never workflow or project state (ADR-0009).
_Avoid_: project memory (implies project-state storage), audit trail

**Retention test**:
The gate for saving a memory: would a fresh session, handed only the repo and the tracker, work differently for this user without it? If no, do not save.
_Avoid_: "might be useful later"
