# ADR 0022: The local model server is single-tenant, CPU-first with optional GPU offload, and the manifest names files over the wire

Status: Accepted — owner-settled in the #678 triage session (2026-11-09); T6 (#678) implements it.

Companion to ADR-0021. ADR-0021 decided YAAH bundles and manages a **local
model server** (glossary term) and that the server, the model manifest, and
the download-on-demand default model are download-on-demand, redistribution-safe
(Qwen3-Embedding-0.6B GGUF, Apache-2.0; llama.cpp is MIT). It left three gate
decisions to T6 (#678): manifest shape, multi-tenancy, and resource policy.
This ADR settles them. Where this ADR's wording refines ADR-0021, the
refinement is surfaced in place rather than applied silently; on approval,
ADR-0021 gets a status note pointing here.

## Decision

### 1. Single-tenant: one model per server instance

A local model server process hosts exactly one loaded model. Serving two
models at once means two processes on different ports, never one process
juggling a shared slot.

The manifest declares many models (future local chat models included), but
the runtime holds one **loaded model** at a time; starting another model on
the same port replaces it, with a health-checked restart when the binary or
its flags change. Per-tenant flag sets are real (chat wants context-size and
GPU layers; embeddings wants `--embeddings` and pooling settings) — one
process cannot apply two tenant flag sets, so multi-tenancy would need a
proxy router in front of N servers. Defer that complexity until a second
tenant actually ships: today's tenant (#1, the embedding model) needs one
slot and nothing else.

This also matches what the rest of the retrieval system can consume: the
vector index records **one** embedding model per build (name + dimensions),
and any mismatch is a degraded state plus explicit rebuild (ADR-0021).
YAAH could not meaningfully use two simultaneous embedding models anyway —
a second tenant is the only future that changes this, and it brings its own
planning effort (ADR-0021).

### 2. Resource policy: CPU-first, offload only when the slots are free

The server starts CPU-only (`-ngl 0` in llama.cpp terms) on a port YAAH
selects. YAAH probes for GPU VRAM headroom and offloads model layers
(`-ngl 99`) only when the free VRAM comfortably exceeds the model's
requirements, re-evaluated at every server start. If headroom disappears —
a chat provider grabbing VRAM mid-run, or any competing process — YAAH does
not fight for it: restart the server on CPU. Degrade, don't contend.

Probe mechanism is an implementation detail (platform tools, per GPU vendor),
with a per-host override in the server config block for hosts where the probe
lies (no GPU despite detection, or offload crashed) — same shape as the
sandbox's `vgpu` override (issue #27's vGPU multi-GPU crash class).

Chat providers are never throttled or preempted: when VRAM is contested, the
local server is the component that yields.

### 3. Manifest names files over the wire, pinned by version

The manifest is an in-code table of model entries (a single editable-in-code
Python list/dict — not a user-editable file, not a network-fetched format):
every entry carries repo/file globs (Hugging Face repo, glob per file for
split GGUFs), a pinned upstream version, revision, sha256 checksums, license,
task hints (`embedding` / `chat`), and the per-model server flags.

By version-pinning this code-side manifest, build N of YAAH always fetches
byte-identical models regardless of when the user installs; a new upstream
version arrives only via a YAAH release. The cost is that staying current
requires a YAAH release; accepted deliberately — reproducibility over
freshness for byte-identical local models.

User-visible model selection: the Settings surface lists manifest models,
users choose from that list. Config stores `server.binary` (`llama` /
`sd` / empty) and `server.model` (manifest id, e.g. `qwen3-emb-0.6b`). An
entry's task hints must cover `embedding` to be selectable as the default
embedding model — task-correctness is manifest-checked, not trusted to the
user. An empty `server.model` falls back to the manifest's default embedding
model. An empty `server.binary` means the local server feature is OFF —
no model, no downloads, no server process; keyword search remains fully
functional.

This **refines ADR-0021** ("the default embeddings endpoint is the bundled
local server") in one respect: local is the default *once the user turns the
server on*, and the out-of-box state is server OFF.

### Companion decision: sd-server is declared tenant #0

The existing stable-diffusion.cpp server (image gen, long-lived child process
managed by YAAH since #421/#441) is declared tenant #0 of the local model
server: the server manager's lifecycle machinery (health check, restart,
clean process-tree shutdown) is written once to serve it, and the embedding
model becomes the second binary under the same manager. This gives T6 a
migration path that proves the manager is generic before a second *model*
tenant ever arrives.

## Considered and rejected

- **llama-server router mode / llama-swap multi-model proxy** — rejected for
  now: router mode's INI format is explicitly unstable upstream, and a proxy
  managing N child servers is real machinery with no second tenant to justify
  it. Revisit when the general local chat runtime plans for real (ADR-0021).
- **User-editable manifest file** — rejected: per-entry server flags, task
  hints, and checksums are contract-critical, not user-tunable knobs; a
  user-editable format would need a schema, validation, and a migration path.
- **Auto-GPU-offload by default** — rejected: VRAM competition with chat
  providers is real on 8 GB-class cards; a self-restarting server that grabs
  VRAM whenever free memory dips is exactly the contending behavior ADR-0021
  and the sandbox vGPU incident (#27) taught us to avoid.

## Consequences

- T6 scope: server manager (port selection, health check, crash restart,
  clean Windows process-tree shutdown), manifest table, downloader (resume +
  checksum verify), CPU/GPU probe, config keys, Settings surface.
- The embeddings config block stays exactly as T5 shipped it — endpoint,
  key, model — and the T6 work sits beneath it: when `server.binary` is
  set and the server is healthy, its `/v1` base URL + manifest model id is
  what fills that block. No client changes needed.
- When the local server is the configured endpoint and it is unhealthy, the
  embeddings client reports unavailable and search degrades to keyword-only
  — the degrade path ADR-0021 already defines, now with a home-server case.
- Revisit triggers: a second real tenant (chat models) arrives → re-decide
  single-tenant (proxy-router machinery becomes justified); llama.cpp
  router-mode config stabilizes upstream → single process could host
  multiple models without a proxy; host probe proven wrong in the field →
  adjust the policy, not the principle.
