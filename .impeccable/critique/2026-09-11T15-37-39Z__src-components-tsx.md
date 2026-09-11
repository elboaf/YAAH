---
target: src/components.tsx (YAAH main UI)
total_score: 32
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
target_identity: "file:C:\\Users\\Administrator\\YAAH\\src\\components.tsx"
target_fingerprint: "sha256:28ca7578b0cf959d32ef0e1f9c76e08182e43b395c05720c3e901ad15517615c"
target_path: "C:\\Users\\Administrator\\YAAH\\src\\components.tsx"
timestamp: 2026-09-11T15-37-39Z
slug: src-components-tsx
---
# Critique: YAAH main UI — post history/layout repair

⚠️ DEGRADED: single-context (no sub-agent/Task tool exposed in this session — Assessments A and B ran sequentially in one context; A was completed and recorded before detector output entered this context)

**Method basis:** full source review (components.tsx, store.ts, index.css, App.tsx) + live headless-Chrome evidence on the running dev app (empty state, skill menu, Settings collapsed/expanded, real 345-row history reload, narrow 760px — .impeccable/shots/crit2-*.png) + detector scan. Source changed since the last run (fingerprint `b60e6cb9…` → `28ca7578…`; commits 4f16708 "History: render tool calls + results", 0028cc9 "turn is the atom"). No ignore.md present.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 4 | Verified live: 0 phantom placeholders in 163 reloaded turns; status line + amber composer still excellent |
| 2 | Match System / Real World | 3 | Traces/ticker/ask card are authored; `md↓`/`sys` jargon and raw enum status labels persist |
| 3 | User Control and Freedom | 3 | Stop, failed-send rollback, confirms all real; mid-stream error banner is a dead end; no undo for deletes |
| 4 | Consistency and Standards | 4 | Dialog family coherent; Esc everywhere; detector found only one contrast warning |
| 5 | Error Prevention | 3 | Attachment rejections speak, IME-safe Enter; provider removal destroys the saved key with no confirm |
| 6 | Recognition Rather Than Recall | 3 | `/s` taught in two places; sidebar row actions are hover-only `title` tooltips — invisible to keyboard |
| 7 | Flexibility and Efficiency | 3 | Tab-complete, Enter contract; still zero global shortcuts (no new-chat/switcher keys) |
| 8 | Aesthetic and Minimalist Design | 3 | Squint-clean, disciplined; composer placeholder is still an 84-char instruction dump; 📎 is the only colored pixel |
| 9 | Error Recovery | 3 | Send-failure path is model-quality; a mid-stream failure pins an undismissable banner and leaves no scar in the transcript |
| 10 | Help and Documentation | 3 | Empty state, settings help lines, skill-menu footer all teach; help is scattered, never summarized |
| **Total** | | **32/40** | **Good, trending up: 24 → 24 → 29 → 32** |

## Design Specificity Verdict

**Authored — and the strongest evidence is what's no longer there.** The last run's defining defect (reloaded history as a wall of fake "thinking" cursors) is not just fixed, it's fixed in YAAH's own language: a reloaded turn is one quiet "1 call · ❯ bash" line that expands to an args/result row — the same grammar as a live turn's collapsed trace. Results merged into calls, expanders beside their chips, provenance readable straight from the audit. No stock chat pattern does this.

**Detector scan** (components.tsx + App.tsx + store.ts + index.html): 1 warning, 1 advisory, both in components.tsx. Warning: `gray-on-color` line 1911, `text-zinc-300 on bg-red-600` (image-remove chip) — real. Advisory: line 316 `rgba(0,0,0,0.35)` — **false positive**, it is the ticker's right-edge mask gradient, invisible by design. The squint/contrast-wash/icon-legibility findings of earlier runs are gone; palette discipline now scans clean.

**Visual overlays:** none this run; evidence from headless-Chrome screenshots + scripted DOM assertions. Console errors in every capture: a single 404 resource (cosmetic).

## Overall Impression

The core promise — watch the machine work, then read the record — is now true in both phases, and the trend (24 → 29 → 32) shows three commits aimed exactly at the lowest heuristics with zero regressions. What remains is perimeter work: the mid-stream failure path, one silent key-destroying affordance in Settings, and copy residue (hover-only tooltips, enum labels, instruction-dump placeholder).

## What's Working

1. **History is audit-grade — verified, not assumed.** DB ground truth (163 turns with 173 calls, 171 results, 0 orphans) rendered as 163 trace lines, 173 expanders, 0 phantom placeholders, collapsed and expanded, at 1440 and 760px.
2. **The turn-as-atom layout.** Results live inside their call's expander beside the trace chip; expanded history is dense but never deeper than two levels.
3. **The dialogs family holds.** Confirm/Prompt/Notice/Settings share Esc + scrim + in-app chrome; delete confirms name what is destroyed.

## Priority Issues

1. **[P1] A mid-stream failure is a dead end that leaves no scar.** `ev.type === 'error'` (components.tsx:1836) pins a red banner (1573) with no dismiss and no retry; the error lives nowhere in the transcript — reload reads as a normal turn. Fix: persist `[turn failed: …]` like `[stopped]`, give the banner dismiss + "Retry turn", or fold into the composer's inline role="alert" pattern. Command: /impeccable harden.
2. **[P1] "remove provider" silently destroys the saved API key.** 10px red text button (components.tsx:1417) in a cramped row; no ConfirmDialog; Save persists config without the key. Fix: ConfirmDialog naming the loss ("The saved API key for {name} will be deleted from config.json") or two-step inline confirm. Command: /impeccable harden.
3. **[P2] Sidebar row actions are hover-only jargon.** `md↓`/`sys`/`✕` (components.tsx:942–968) are title-tooltip-only; keyboard focus never reveals them. Fix: one `⋯` affordance opening a labeled menu, or text buttons with aria-labels. Command: /impeccable clarify.
4. **[P2] The composer placeholder still does the empty state's job.** 84 chars of instruction in the least roomy surface; the empty state and skill-menu footer already teach it. Fix: `placeholder="Describe a task…"`. Command: /impeccable distill.
5. **[P3] 📎 is the only full-color pixel in the console.** OS emoji in a monochrome currentColor glyph language. Fix: inline SVG paperclip. Command: /impeccable polish.

## Persona Red Flags

**Alex (Power User):** No keyboard paths at all (no Ctrl+K switcher, no Ctrl+N); must mouse to Settings. The skill menu's keyboard contract (↑↓ · Tab · Esc — Enter sends) has no equivalent elsewhere.

**Jordan (First-Timer):** Served by the empty state, amber no-provider button, speaking rejections, draft restore. But `md↓`/`sys` are unmappable, and a mid-stream death offers a pinned error with no next step.

**Supervisor Sam:** Audit trail trustworthy at rest — one loophole: an errored turn reloads as a clean transcript (P1 #1's "no scar" problem). The record should never look healthier than the run was.

## Minor Observations

- The ✕ that dismisses a dialog lives inside the dialog surface, appears on hover only — unclear it's a close.
- Settings provider row is still a double-target (radio inside row-button); aria-expanded + stopPropagation make it work, but the row never says "click to expand" beyond a 10px chevron.
- Last run's deletable debris remains: `<style>{'button[style] { cursor: default; }'}</style>` (682) and dead `{previewPath === null && tree.length > 0 && null}` (683).
- Amber rejection notices drift from DESIGN.md semantics (amber = machine at work); error/red or neutral zinc would read truer.
- Conversation titles leak command syntax ("/sdiagnostic ping…"); trim a leading `/s …` when titling.
- Status line shows raw enums (`running-tool`, `error`).
- Run artifacts: scripts/crit-evidence2.mjs (keep or delete), .impeccable/shots/crit2-*.png; the detector advisory is a candidate for `impeccable ignores add-value rgba(0,0,0,0.35)`.

## Questions to Consider

- What if the most recent trace in reloaded history auto-expanded, so reopening a session shows proof of work before any click?
- What would a keyboard-first layer look like (Ctrl+K switcher, Ctrl+N) given the skill menu already sets the keyboard contract?
- Should a failed turn leave a visible marker in the transcript so the audit trail never lies by omission?
