# ADR 0018: Sub-agent model scope — chat pin over global fallback, frontmatter layer removed

Date: 2026-10-12
Status: Accepted
Driven by: #383 (spec, settled in a grilling session), #51/#76 (the per-chat
scope family it joins), #132 (the complete-or-empty rule it follows).

## Context

Sub-agent model routing had three layers: a per-agent `provider:` line in an
agent definition's frontmatter (strongest), the global `subagent_provider`
setting (a bare provider name — no model choice), and inheriting the parent
conversation's model (the default). The per-agent frontmatter layer had zero
users — no agent file has ever existed in any install — and was documented
only inside the code and one Settings card sentence. The global setting named
a provider but not a model, so it could not express "run subs on the small
model on server two", only "run subs on server two's configured model".

Meanwhile the chat header already carried a mature per-chat scope family:
model and effort pickers, stored complete `provider::model` or empty, written
through to the owning scheduled agent, repaired by the #132 boot pass. The
user's actual workflow — keep the parent's prompt cache hot by sending
sub-agents to a second local server — was expressible only globally, never
per chat.

## Decision

Sub-agent model resolution becomes, in order:

1. **Per-chat pin** — a third select ("sub-agent") in the chat model bar,
   stored `conversations.subagent_model` / `agents.subagent_model`,
   complete `provider::model` or empty ("sub-agent: auto"). Agent-pinned
   chats write through to the owning agent, exactly as model/effort do.
2. **Global fallback** — the existing `subagent_provider` setting, retained
   but upgraded from a bare provider name to full model discovery (grouped
   dropdown, complete scopes). Existing bare values (e.g. `llamacpp-2`)
   keep working via resolve-time qualification — the #132 back-compat
   branch, not a new mechanism.
3. **Inherit parent** — both empty: sub-agents stream on the chat's own
   resolved model.

The per-agent `provider:` frontmatter override is **deleted** — field,
parser line, resolver branch. Its "always route this agent type cheap,
everywhere" use case can return as a real feature if an agent file ever
needs it; a no-user mechanism is not kept on spec.

The primary model selection surface is untouched: the chat's own model and
effort, the sidebar default, and draft pinning behave exactly as before.
The internal memory-writer sub-agent resolves global → inherit; it is
harness machinery, not a delegation a chat pinned.

## Consequences

- Three scopes share one vocabulary and one set of rules (GLOSSARY
  "Model scope" / "Sub-agent scope"): complete or empty, never bare; the
  repair pass covers every column; a scope naming a dead provider fails
  that spawn with the existing structured error instead of re-routing.
- Unpinned installs that relied on the global setting see no change;
  installs that never set it gain a way to pin per chat.
- Parent and up to four concurrent sub-agents can now target the same
  single-slot local server (the pin's point); slot contention is accepted
  with eyes open, as in the direct world (ADR-0017).
