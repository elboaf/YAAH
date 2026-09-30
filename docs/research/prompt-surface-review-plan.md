# Prompt-Surface Review — Plan

Operational plan for the full prompt-surface review agreed in the grilling
session of 2026-09-30 (this document; the fact base lives in
[prompt-surface-inventory.md](./prompt-surface-inventory.md); the spec and
tickets live on the issue tracker as children of the spec issue).

## Decisions (settled in interview)

| Decision | Value |
|---|---|
| Review axes | **Correctness/consistency + token economy.** Subjective prompt-engineering quality is out of scope. Every finding is provable (prompt-vs-code claim, or fragment-vs-fragment contradiction, or measured size/duplication). |
| Deliverable | **Findings doc + agent-ready GitHub issues.** The review never fixes code; each actionable finding becomes its own issue, ready for `/implement` pickup. |
| Scope | **Core + injected content + mechanisms.** Core = base prompt, conditional fragments, all 37 self-defined tool descriptions, auxiliary prompts (compaction, title, sub-agent scaffolding). Injected = AGENTS.md notes, memory block, skill bodies/index. Mechanisms = injection paths themselves + the get_help lazy docs tier + in-band model-directed strings. Excluded (wiring-only, not wording): vendored windows-mcp prompts, prompts/scheduled-implement.md, docs/agents/*.md, the 34 bundled skill bodies. |
| Verification | **Static review + render matrix.** The manifest harness (ticket 1) assembles real prompts for every configuration combination; reviewers read rendered bytes, not reconstructions. No model-in-the-loop behavioral testing. |
| Baseline | **Main HEAD at execution time**, recorded in the findings doc. Open PRs' prompt deltas are reviewed at their own merge time. |
| Process | **One orchestration session, parallel sub-agents per ticket.** Tickets are worked blockers-first; findings are synthesized into one doc, then issues are filed. |

## The seam

One new seam: **the manifest-harness boundary**. Review tooling and drift tests
call the real prompt-assembly entry points and consume the per-combination
manifests they emit. Nothing downstream ever re-implements or hand-reconstructs
assembly. Reviewers (tickets 2–5) read manifest artifacts; existing
substring-test style in backend/tests remains prior art for any content guards.

## Tickets (tracer-bullet order)

1. **Manifest harness** — no blockers. Committed tooling that drives the real
   assembly entry points across all configuration combinations (local/remote ×
   plan/normal × skills present/absent × memory present/absent × screenshot
   toggle × override × compaction-fired × offline-remote × scheduled
   sandbox-only), emitting one manifest per combination: section list, byte
   sizes, content hashes, and the fully rendered text. Plus a determinism test.
   Demoable on its own.
2. **Review: base prompt + conditional fragments** — blocked by 1.
3. **Review: tool descriptions + get_help docs + in-band strings** — blocked by 1.
4. **Review: sub-agent prompt path + auxiliary prompts** — blocked by 1.
   Includes verifying the phantom `git_*` tools finding from the inventory
   (§6.1) against the rendered sub-agent manifest.
5. **Review: injected content + injection mechanisms** — blocked by 1.
   Wiring-only for content YAAH does not author.
6. **Synthesis** — blocked by 2–5. Findings doc (per-finding: evidence
   file:line, render-matrix proof, suggested fix, severity) + duplication and
   contradiction matrix + token-economy table (per-combination sizes, duplication
   tax). Then one agent-ready GitHub issue per actionable finding, labeled
   `ready-for-agent`.

## Known review targets from the inventory walk

Carried as leads, not conclusions — each must be verified against rendered
manifests by the owning ticket:

- Sub-agent prompt advertises `git_commit`/`git_push`/… but no `git_*` executor
  exists locally (remote-forward-only). Parent prompt does not list them.
- Test-environment guidance near-verbatim in two places; "git must never open
  its editor" rule in three.
- gh-CLI preference stated three times.
- "Invoked skills" wrapper text duplicated with wording drift between the two
  skill-injection paths.
- windows-mcp playbook stated three times (sandbox section, sandbox_run
  description, per-input-tool host note).
- `SAY_MAX_CHARS` mirrored Python/TS — drift breaks transcript stripping.
- Base prompt renders ≈24 KB on a Windows host before any conversation content;
  per-combination sizes to be measured exactly by the harness.

## Out of scope

- Implementing fixes for any finding (they become issues from this review).
- Subjective prompt-engineering quality (clarity, tone, ordering aesthetics).
- Behavioral/model-in-the-loop testing of whether instructions land.
- Vendored upstream prompt text, ops prompts, docs/agents wording, bundled
  skill bodies (wiring review only, per ticket 5).
- Prompt deltas from the five currently-open PRs (reviewed at their merge).
