# YAAH

An agent harness where multiple chats and sub-agents can work in the
same workspace. The vocabulary below is about how their work lands in per-chat
worktrees (ADR-0010).

## Language

### Workspace

**Primary worktree**:
The workspace's checkout as the human opened it — the only tree agents
never touch. The draft/workspace-level git endpoints remain its tools.
_Avoid_: main tree, shared tree, "the checkout" unqualified

**Chat worktree**:
The per-chat git worktree a chat's runs work in, materialized on first
write at the chat's selected branch. Selector flips and landing merges
happen inside it, so conflicts are that chat's private problem.
For a chat aimed at a remote workspace, the worktree
materializes on the remote host — same lifecycle, client-driven
(#305).
Retired when its work lands and the tree is clean — a worktree exists
only while its work is in flight; branches are never held open by
finished work. Trees of dead chats retire the same way past an age
threshold. See ADR-0010 (supersedes ADR-0008; land-means-clean
lifecycle per #329).
_Avoid_: session worktree, `agent/*` branch

**Remote workspace**:
A workspace whose tree lives on another host — a second YAAH instance
reached through its exec channel (`remote:<host>:<path>`). Everything
about the chat is local except the tree: workspace tools execute
there, and the chat's worktree materializes there too (ADR-0010,
#305).
_Avoid_: treating it as metadata-only; assuming the repo exists on the
client; hiding features instead of stating where they run

**Host offline**:
The explicit state a remote workspace’s git surfaces show when the host
cannot be reached over the channel (#333) — rendered as its own chip,
never read as data (a missing repo is data; unreachability is not).
_Avoid_: silent absence; conflating a non-repo workspace with an
unreachable host

**Branch selector**:
The per-chat stored value naming the branch a chat's work lands on —
the single source of truth for landing. Set at chat creation from the
destination card's pick or the workspace's current branch; it never
follows the primary worktree afterward. Every pin carries an **origin**:
_explicit_ (a user pick — destination card, chip, or the branch_select
tool) or _inherited_ (the workspace's branch at creation; also what
legacy NULL rows lazily pin as on first read). Picking the inherited
branch from the chip adopts it into an explicit pick. A pin whose
branch no longer exists in the workspace's repository is **stale** — surfaced, never silently
re-created. Flipping it checks out inside
the chat's own worktree and refuses while that worktree is dirty.
Agents update it via a tool on user request; a switch request to an
agent is a selector change, never a primary-tree checkout.
_Avoid_: global checkout, "switches the branch for every chat", unset
(every chat in a git workspace has a branch), inheriting from the
primary worktree

**Landing**:
A run's merge of its scratch-worktree branch onto the chat's selected
branch, inside the chat's worktree. On the user's explicit choice at
the #349 end-of-work landing ask, agents execute the landing —
including into a branch checked out in the primary tree (e.g. master)
under ADR-0015's resolution contract: dirty-primary WIP is preserved
on a local `wip/` branch (never pushed), merge conflicts resolve with
the run-branch side winning, every touched file reported; only a
mid-merge/rebase/cherry-pick primary still freezes as manual-only.
Typed "land it" / "scrap it" remain the manual fallback.
_Avoid_: merge-back, auto-merge, landing without an explicit user
choice, `git branch -f` on a checked-out branch

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

**Fact bases**:
Where prompt-surface truth lives. Measured facts — sizes, section breakdowns, rendered bytes — live in the committed manifests under `backend/prompt_manifests/`, regenerated and byte-drift-guarded against the code. Orientation lives in this section and in ADR-0011. There is no hand-pinned line-number document; those rot (ADR-0011 records the deletion of the prose inventory/findings docs).
_Avoid_: `docs/research/prompt-surface-*.md` (deleted); re-deriving sizes by hand instead of reading a manifest

### Memory

**Memory**:
The per-project store of durable facts about the owner — user facts and working feedback — injected into every session's prompt. It models the owner, never workflow or project state (ADR-0009).
_Avoid_: project memory (implies project-state storage), audit trail

**Retention test**:
The gate for saving a memory: would a fresh session, handed only the repo and the tracker, work differently for this user without it? If no, do not save.
_Avoid_: "might be useful later"

### Voice

**Spoken briefing**:
The short text the voice channel reads aloud for a completed model emission. The model authors it inside a `<say>` tag on its message; when the tag is missing or unusable, a heuristic (first/last sentence) fallback speaks instead. The briefing lives outside the conversation transcript — it is stripped from stored chat content, shipped on its own `say` wire event, and persisted on the row for reloads and export. The `spoken_line` normalization (numbers spelled out, markdown flattened, length cap) is a speak-time pass over the briefing; the raw model text is what persists.
_Avoid_: conflating the spoken briefing with the chat transcript (it is never part of it); conflating the briefing with the `spoken_line` normalization output (one is authored content, the other a TTS rendering of it); "say message" (the briefing is not a message)
