---
target: src/components.tsx (YAAH main UI)
total_score: 24
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 2
target_identity: "file:C:\\Users\\Administrator\\YAAH\\src\\components.tsx"
target_fingerprint: "sha256:d592c91a86df9e634f7f71e2e41767cc7112bafd37ab92f384ea44a37c48f132"
target_path: "C:\\Users\\Administrator\\YAAH\\src\\components.tsx"
timestamp: 2026-09-11T13-03-44Z
slug: src-components-tsx
---
# Critique: src/components.tsx (YAAH main UI) — re-run, unchanged source

Method: DEGRADED — single-context (no sub-agent/Task tool exposed in this session; sequential A→B fallback per critique flow)

Freshness: source fingerprint sha256:d592c91a…c48f132 matches the 2026-09-11T05-18-31Z run exactly; git working tree clean. This is a deliberate re-measurement of unchanged code.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Model switch, tree refresh, and preview load give no feedback |
| 2 | Match System / Real World | 3 | "md↓" and "sys" row actions are cryptic even for devs |
| 3 | User Control and Freedom | 3 | No undo for deletes; no edit/resend of a sent message |
| 4 | Consistency and Standards | 2 | Native confirm/prompt/alert clash with the app's own modal system |
| 5 | Error Prevention | 2 | Oversized/binary attachments dropped silently |
| 6 | Recognition Rather Than Recall | 3 | /s skill invocation is undiscoverable; workspace must be typed from memory |
| 7 | Flexibility and Efficiency | 2 | No keyboard paths for new chat / conversation switch / settings |
| 8 | Aesthetic and Minimalist Design | 3 | Dense but disciplined; sidebar stacks many bare controls |
| 9 | Error Recovery | 2 | Error banner is a dead end: no retry, no dismiss, no resend |
| 10 | Help and Documentation | 1 | No in-app help; empty state speaks developer jargon |
| **Total** | | **24/40** | **Needs work — solid core, weak edges** |

## Design Specificity Verdict

**Authored, not category-interchangeable — on the core loop.** The ticker→trace collapse, the orange ask-card channel, the status line, and per-tool glyph identity could not be transplanted to a generic chat app unchanged; they are the product's transparency thesis made visible. The periphery is another story: the Settings modal is a generic form wall, the empty state is two lines of developer jargon, and the sidebar is a standard chat sidebar. Core: authored. Periphery: interchangeable.

**Deterministic scan** (`impeccable detect --json src/App.tsx src/components.tsx`): 2 findings, identical to the prior run.
- `gray-on-color` (warning) at components.tsx:1554 — **false positive**: `text-zinc-300` pairs with `bg-zinc-700` (base) and `hover:text-white` pairs with `hover:bg-red-600`; the flagged combination never renders.
- `design-system-color` (advisory) at components.tsx:316 — ticker fade mask `rgba(0,0,0,0.35)` undocumented in DESIGN.md; legitimate, should be added to the sidecar or ignore list.

**Visual overlays**: not available — no browser automation tool exposed this session; the CLI scan is the machine evidence.

## Overall Impression

Unchanged verdict, now confirmed by an identical fingerprint and detector output: the core agent loop is genuinely well-designed, and everything the public-distribution claim makes load-bearing — silent failures, native dialogs, dead-end errors, undiscoverable features — is what holds the score at 24. The single biggest opportunity remains: make failure states as well-designed as the working state.

## What's Working

1. **Ticker→trace collapse** — loud while working, one quiet line when done; the fade mask keeps it bounded without a scrollbar.
2. **The ask card** — a distinct orange channel with option chips, a dashed "Something else…" escape hatch, and a pulsing marker; "busy" and "waiting on you" never blur.
3. **Disciplined color economy** — near-monochrome zinc with color reserved for state; the detector found almost no drift (2 findings, one a false positive).

## Priority Issues

1. **[P1] Silent attachment failures** — `addImageFile` drops images >5MB or non-images with no feedback; `readDroppedFiles` silently skips text files >200KB and binary-looking files. The user believes the agent received the content.
   **Why it matters**: the agent works from incomplete context and the user can't know the drop happened — a trust failure in a product whose thesis is transparency.
   **Fix**: on drop/paste/attach, reject with an inline chip or toast: "image.png skipped — 7.2 MB exceeds the 5 MB limit."
   **Suggested command**: `/impeccable harden`

2. **[P1] Native browser dialogs break the crafted surface** — `confirm()` for file/conversation delete, `prompt()` for the system-prompt override, `alert()` for export/delete failures — while the app ships its own modal system. SettingsModal also lacks the Esc handler PreviewModal has.
   **Why it matters**: native dialogs are unstyled, block the event loop, and jar against the terminal aesthetic; inconsistent Esc behavior reads as bugginess.
   **Fix**: replace all four with in-app dialogs on the existing modal pattern; add Esc/backdrop handling to SettingsModal.
   **Suggested command**: `/impeccable polish`

3. **[P2] Error states are dead ends** — the stream-error banner has no dismiss and no retry; a failed send leaves the user's message in the log with no resend; export/delete failures surface as alert text.
   **Why it matters**: the user's only recovery is retyping the whole prompt — the exact abandonment moment.
   **Fix**: dismissible banner with a Retry action; on send failure, keep the draft in the composer with the error inline.
   **Suggested command**: `/impeccable harden`

4. **[P2] /s skills and workspace are recall-heavy** — the composer placeholder never mentions `/s`; the only discoverability is the sample skill's text. Workspace requires typing a path (browse works only inside Tauri) with a hover-only tooltip.
   **Why it matters**: PRODUCT.md commits to public distribution; first-run users will never find skills and will stumble on workspace setup.
   **Fix**: mention `/s` in the empty state and placeholder; add a workspace hint or recent-workspace list on first run.
   **Suggested command**: `/impeccable onboard`

5. **[P2] Settings modal is a flat wall** — every provider (5 fields each), preset chips, temperature, max tokens, and max steps render at once — a decision point well past 4 simultaneous choices.
   **Why it matters**: configuration is the first thing a BYO-key user must complete; a wall of fields is where they abandon.
   **Fix**: group into "Providers" and "Generation" sections; collapse per-provider fields behind the provider name; surface the active provider first.
   **Suggested command**: `/impeccable distill`

## Persona Red Flags

**Alex (Power User)**: No keyboard shortcut for new chat, conversation switching, or settings; Esc closes the preview but not Settings; Enter-to-send and skill-menu arrow navigation are good. Attachment limits invisible until violated. Will grumble but stay.

**Jordan (First-Timer)**: "/s" undiscoverable; "md↓" and "sys" row actions are unlabeled jargon; the no-provider state hides inside a select option; workspace field expects a path with no guidance. Will abandon at first-run configuration.

**Supervisor Sam** (project-specific, from PRODUCT.md's transparency principle): wants to audit what the agent did — the trace line is good, but args/results render as raw JSON dumps; the error banner without recovery breaks the trust contract exactly when proof of control matters most.

## Minor Observations

- Empty-state copy explains mechanics in developer terms; it should teach the first action instead.
- The stray `<style>{`button[style] { cursor: default; }`}</style>` in FilesPanel leaks a global-ish rule; the adjacent `{previewPath === null && tree.length > 0 && null}` is dead code.
- CopyButton flashes "copied!" with no aria-live announcement; the status dot has no accessible name.
- The status line shows raw enum spellings ("running-tool").
- Detector advisory: add `rgba(0,0,0,0.35)` (ticker mask) to `.impeccable/design.json` or the critique ignore list.

## Questions to Consider

- What if the composer — the one element every session touches — were the visual anchor of the app rather than visually equal to the panels around it?
- What would a confident first run look like: could the empty state teach workspace + /s in a single glance?
- Should a finished turn that edited files auto-expand its trace to show the diffs, so proof of work is the default rather than a click away?
