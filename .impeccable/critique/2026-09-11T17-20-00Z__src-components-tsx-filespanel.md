# Critique: FILES side panel (src/components.tsx #FilesPanel)

⚠️ DEGRADED: single-context (no sub-agent tool exposed; assessments ran sequentially in one context)

Method: source review of TreeRow/FilesPanel/PreviewModal + live headless-Chrome evidence (expanded tree, preview modal, context menu, collapsed rail, 1000px viewport — .impeccable/shots/sp-1…5.png) + detector scan. Two initial "dead interactions" were script artifacts (row textContent includes the caret glyph); re-verified working before critique.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | No loading state; slow tree load renders the empty state's text — a lie. Errors persist after a workspace change fixes them |
| 2 | Match System / Real World | 3 | Tree/caret/density read like an instrument panel; bullet-for-file nonstandard but consistent |
| 3 | User Control and Freedom | 3 | Panel state persists; no keyboard path to any row (context menu mouse-only) |
| 4 | Consistency and Standards | 2 | Context menu ignores the Esc contract; stale open-file-highlight dead code |
| 5 | Error Prevention | 4 | Delete names path + permanence in the standard ConfirmDialog |
| 6 | Recognition Rather Than Recall | 3 | Empty state honest (but shown during loading); open file not marked |
| 7 | Flexibility and Efficiency | 2 | No refresh on turn end; panel ignores agent file-writes mid-session |
| 8 | Aesthetic and Minimalist Design | 4 | Squint-clean, hairline-separated, rail is a signature |
| 9 | Error Recovery | 2 | Raw backend exception; retry only via header refresh |
| 10 | Help and Documentation | 3 | Empty state teaches the fix; title tooltips only |
| **Total** | | **28/40** | **Fair — solid core, shallow state coverage** |

## Design Specificity Verdict

Authored but half-finished. The rail and collapse persistence are recognizably YAAH's terminal language. But the state model stops at "has tree / has no tree" — no loading, stale, or current-file concept, which every neighboring surface treats as first-class. The dead `<style>` hack and `{previewPath === null && tree.length > 0 && null}` confirm an open-file highlight was started and abandoned.

Detector: 1 warning + 1 advisory, both outside this panel (image-remove chip contrast; ticker mask gradient). No findings attributable to the side panel.

## What's Working

1. Collapse is first-class — rail + vertical label + persisted flag.
2. Delete is honest — ConfirmDialog names path and permanence.
3. Density discipline — 11px rows, 12px indent steps, hairlines, no shadows.

## Priority Issues

1. [P1] No loading state — the empty state lies during a slow tree load. Fix: three-state render (loading/error/empty/tree).
2. [P1] Context menu ignores Esc. Fix: window keydown → setMenu(null) while open.
3. [P2] Errors never clear after a successful refresh; no retry link. Fix: clear on success + retry affordance.
4. [P2] Panel is stale the moment the agent writes a file. Fix: refetch on running-tool → idle transition.
5. [P3] Dead code (`<style>` hack, highlight stub) and no keyboard path to the context menu.

## Minor Observations

- Preview modal header: language/copy at 10px, nearly invisible.
- Context menu bottom clamp (120px allowance) under-shoots for 2 items; harmless.
- Caret column has no fixed width; slight indent jitter on mixed rows.
- Menu lacks role=menu/menuitem semantics; rail buttons lack aria-labels.

## Questions to Consider

- Should the previewed file be highlighted in the tree (the abandoned feature)?
- Should the panel mark files the agent touched this turn (tool spectrum glyph)? The panel's killer feature waiting to exist.
- Is bullet-for-file pulling its weight?

## Disposition

All five priority issues fixed in commits ee6f999 (state model + a11y) and the follow-up (keyboard path to context menu). Verified: tsc clean, 56/56 pytest, ESC-MENU PASS live, side panel evidence re-run clean. Snapshot closed by the polish pass.
