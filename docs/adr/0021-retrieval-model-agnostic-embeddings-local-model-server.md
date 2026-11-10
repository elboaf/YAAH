# ADR 0021: Retrieval is model-agnostic over an OpenAI-compatible embeddings endpoint, with a bundled local model server as the default host

Date: 2026-10-30
Status: Accepted. Amended by ADR-0022 (2026-11-09): the out-of-box state of
the local server is OFF — "the default embeddings endpoint is the bundled
local server" applies once the user enables the server; nothing downloads
and no process starts without that explicit consent. ADR-0022 also settles
the T6 gate decisions this ADR left open (tenancy, resource policy, manifest
shape).
Driven by: conversation-search/RAG planning session (Oct 2026); Phase 2a/2b of
the retrieval roadmap. The owner's learning goal is a stated requirement: YAAH
exists partly to teach its owner modern AI implementations, and RAG was chosen
for its teaching value even though agent retrieval was judged achievable
without it.

## Context

Conversation search was a linear substring scan over one conversation's
messages — slow, unranked, and pinned to the conversation an agent sits in.
The roadmap upgrades it in phases: a keyword index first (FTS5 trigram), then
semantic (embedding) retrieval, then hybrid fusion of the two.

For the semantic layer, the embedding model had to be chosen. The three
candidate homes were a hosted API (transcripts leave the machine, paid per
token), an in-process ONNX model (deepest packaging cost), and a locally
served open-weight model. Separately, the owner declared a product ambition:
YAAH should eventually ship local chat models — a general local runtime — not
merely rent providers from a config file.

## Decision

1. **YAAH speaks one embeddings contract: the OpenAI-compatible
   `/v1/embeddings` shape.** Any model reachable through that shape is
   supported; there is no YAAH-side model coupling. It is the user's
   responsibility to point the endpoint at a *semantic* model.

2. **Vectors are model-labeled and never silently mixed.** The vector store
   records the embedding model name and dimensions that produced it. A
   configured model that does not match the index is a degraded state:
   keyword search continues to work, semantic results report unavailable,
   and a Settings surface offers an explicit, user-triggered rebuild
   (full re-embed). Rebuilds never run automatically at boot. Vectors do
   not self-identify their source model — the stored label is the only
   guard, and it is advisory: a different model under the same name and
   dimensions is undetectable.

3. **YAAH bundles and manages a local model server, named generically.**
   The server manager and model manifest are built as "local model server,"
   not "embeddings server": the embedding model is its first tenant, not its
   definition. This leaves the declared general-runtime ambition (local
   chat models) a seam to grow into rather than a rewrite. The ambition
   itself is recorded as a tracking issue and gets its own planning effort
   after retrieval ships — it must not grow inside the retrieval roadmap.

4. **The default model ships via download on demand** (Qwen3-Embedding-0.6B
   GGUF, Apache-2.0; llama.cpp is MIT — both redistribution-safe), following
   the existing download-on-demand precedent. Installers stay small; users
   who never touch semantic search never pay the bytes. The default
   embeddings endpoint is the bundled local server; users may point it at
   any endpoint speaking the same contract.

## Consequences

- Swapping embedding models is a supported operation with a known cost:
  an explicit index rebuild. Model choice is a user decision, not a
  codebase decision.
- No transcript text ever leaves the machine under the default setup.
- The harness carries a long-lived child process (the model server) with
  the usual lifecycle obligations: port selection, health checking,
  crash restart, clean shutdown.
- The keyword (Phase 1) index is deliberately model-free: semantic
  degradation never touches it.
