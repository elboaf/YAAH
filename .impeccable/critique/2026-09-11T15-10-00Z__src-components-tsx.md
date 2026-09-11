---
target: src/components.tsx (YAAH main UI)
total_score: 29
max_score: 40
na_heuristics:
p0_count: 1
p1_count: 3
target_identity: "file:C:\\Users\\Administrator\\YAAH\\src\\components.tsx"
target_fingerprint: "sha256:b60e6cb951485784669ad851ba09a4aaec10e0599f75ebf501be897604087d47"
target_path: "C:\\Users\\Administrator\\YAAH\\src\\components.tsx"
timestamp: 2026-09-11T15-10-00Z
slug: src-components-tsx
---

# Critique: YAAH main UI — post-onboard/dialogs run

Method: LIVE EVIDENCE (headless Chrome against the running dev app, desktop + narrow viewports, interactive states: empty, skill menu, Settings collapsed/expanded, live streaming turn, reloaded conversation history). Backend data inspected via live API. This run supersedes the 24/40 DEGRADED run from earlier today; source changed since (fingerprint `d592c91a…` → `b60e6cb9…`, commit 07a4f99).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 4 | Live state is now excellent (amber composer, Stop-in-place, status line); reloaded history shows phantom "thinking" states |
| 2 | Match System / Real World | 3 | Ask card and ticker are authored; "md↓"/"sys" row jargon persists |
| 3 | User Control and Freedom | 3 | Failed-send rollback is real; no undo for deletes, no edit/resend |
| 4 | Consistency and Standards | 4 | In-app dialogs land; SettingsModal now matches PreviewModal Esc behavior |
| 5 | Error Prevention | 3 | Attachment rejections now speak; confirm dialogs name what is destroyed |
| 6 | Recognition Rather Than Recall | 3 | /s in empty state + placeholder; workspace path still recall-heavy |
| 7 | Flexibility and Efficiency | 3 | IME-safe Enter, Tab-complete; no global keyboard paths yet |
| 8 | Aesthetic and Minimalist Design | 3 | Dense and disciplined; composer placeholder is a 96-char instruction dump |
| 9 | Error Recovery | 3 | Send failure restores draft inline; mid-stream error banner still a dead end |
| 10 | Help and Documentation | 3 | Empty state teaches the real first run; skill-menu footer documents its own keys |
| **Total** | | **29/40** | **Good — up from 24; one new P0 in history rendering** |

## Design Specificity Verdict

**Authored — and now visibly so on the periphery too.** The re-critique's question is whether commit 07a4f99 ("Onboard, in-app dialogs, and distilled Settings") fixed the right things in the right voice, and the evidence says mostly yes: the Settings accordion, the DialogShell family, and the 3-step empty state are recognizably YAAH's terminal-instrument language (hairlines, mono micro-labels, indigo only where a skill is named, blue only on human actions), not stock patterns pasted in. The core signatures — ticker→trace, ask card, status line — remain the strongest identity elements.

**Live visual evidence** (headless Chrome, `.impeccable/shots/`):
- `02-skill-menu.png` — `/s` menu renders exactly per the design system: indigo mono names, 10px zinc descriptions, keyboard contract in the footer. Findable in one step from the empty state.
- `04-settings-expanded.png` — the accordion works: active provider first with radio + "key saved", collapsed rows summarized, generation grouped under an uppercase header. The wall of fields is gone. One flaw (below).
- `05-send-failure.png` / `07-narrow-760.png` — the live turn: amber composer border while streaming, Stop swapped in-place (no layout shift), pulsing block cursor, amber status dot "working". The state shown in the composer itself, exactly what the last composer critique demanded.
- `08-turn-result.png` — **the run's defining defect, see P0.**

**Heuristic drift from last run:** +1 status, +1 consistency, +1 error prevention, +1 help. −0. The fixes were aimed at exactly the heuristics that were lowest; nothing regressed.

## Overall Impression

The failure paths that held the app at 24 are now designed: attachments speak when rejected, the draft survives a failed send, native dialogs are gone, and first-run has a path. The app's happy path and its recovery paths finally have the same craft level. What remains is one genuine defect the previous critique's static method could not see (it required reloading a real conversation), plus polish-level residue.

## What's Working

1. **Composer under stream** — amber border, Stop in place, status line, all visible where the user is looking; narrow viewport (760px) degrades gracefully to two panes with no overflow.
2. **The dialogs family** — ConfirmDialog names what gets destroyed ("…and all its messages will be removed. This cannot be undone."), PromptDialog is multi-line with Ctrl+Enter, SettingsModal closes on Esc. Consistency heuristic +2 in one commit.
3. **First-run path** — the empty state's 3 steps match the actual order of operations (workspace → provider → task), the no-provider state escaped the select into an actionable amber button, and the workspace hint explains consequence ("the agent can't see your files yet"), not mechanics.

## Priority Issues

1. **[P0] Reloaded conversations render as a wall of empty "AGENT" placeholders.** `loadHistory` (store.ts:226) attaches `toolCalls` only to `role === 'tool'` rows, so the 45 empty-content assistant rows from a real agent turn each render as `▌` pulsing-cursor placeholders (MessageView's `!msg.content && !msg.toolCalls?.length` branch). Verified live: conversation 291 shows 40+ identical "AGENT ▌" blocks instead of one trace per turn.
   **Why it matters**: history is the product's transparency thesis at rest — the audit view Supervisor Sam depends on is unreadable noise, and every placeholder *animates* as if the agent were thinking, which is a false status signal.
   **Fix**: in `loadHistory`, attach `tool_calls` to assistant rows too (each turn's calls collapse into a TraceLine), or drop empty assistant rows that precede their tool messages and let the tool rows carry the trace.
2. **[P1] Settings accordion rows are double-targets** — the whole row is a `<button>` that expands, but it contains a radio input (activate provider). Clicking the radio region is a nested-activation trap (`stopPropagation` saves it, but keyboard users tabbing to the radio then hitting Space also toggles the accordion's focus state); more importantly nothing about the row says "click to expand" except a 10px chevron.
   **Fix**: make the expand affordance explicit (name + chevron as the button, radio visually separate), or add `aria-label="Expand {name} settings"` to the row button.
3. **[P1] Mid-stream error banner is still a dead end** — `ev.type === 'error'` sets the global red banner (components.tsx:1573) with no dismiss and no retry; only the *send-failure* path got the restore-and-dismiss treatment. A turn that dies at step 6 of 10 leaves the transcript frozen mid-turn with the error pinned above the composer until the next send.
   **Fix**: give the banner a dismiss and a "Retry turn" action, or fold stream errors into the same inline composer pattern the send-failure path uses.
4. **[P2] Composer placeholder is a 96-character instruction dump** — "(drop/paste/attach images or text files; type /s to load a skill)" does the empty state's job in the one element that has the least room for it, and vanishes on first keystroke.
   **Fix**: `placeholder="Describe a task…"`; attachment affordances are already discoverable (📎 button, drag hint could live on the 📎 title), `/s` is taught by the empty state and the skill menu footer.
5. **[P2] Sidebar row actions are still cryptic** — `md↓` and `sys` (components.tsx:951, 960) rely on hover-only tooltips; `✕` for delete is fine. Jordan can't map "md↓" to export.

## Persona Red Flags

**Alex (Power User)**: Keyboard story unchanged — no shortcut for new chat/settings/conversation switch; must mouse to Settings. Will hit the P0 the first time they reopen yesterday's session.

**Jordan (First-Timer)**: Genuinely served now: empty state tells them what to do, no-provider state opens Settings in one click, rejections speak. The P0 will convince them the app is broken the moment they reopen a conversation.

**Supervisor Sam**: The trace/ticker system remains the strongest part — but P0 means the audit trail is only correct *while the turn is live*. The moment it's reloaded from the DB, the evidence is a wall of fake "thinking" cursors: the exact inversion of the transparency promise.

## Minor Observations

- FilesPanel still ships `<style>{'button[style] { cursor: default; }'}</style>` (components.tsx:682) — a global-ish rule leaking from a component, plus dead `{previewPath === null && tree.length > 0 && null}` on 683. Two lines, trivially deletable.
- The reloaded-conversation user bubble at the top of 08-turn-result.png renders with no visible left boundary (right-aligned, borderless background) — fine live, slightly disorienting at the top of a long scroll.
- `run-pulse` on the reloaded placeholders is the worst symptom of P0; once history is fixed, consider `prefers-reduced-motion` handling for both animations (chip-in, run-pulse) as a separate a11y pass.
- Status line still shows raw enum `running-tool` → only after error states; minor copy polish.
- puppeteer-core was installed `--no-save` for evidence; `scripts/crit-shots*.mjs` and `.impeccable/shots/` are artifacts of this critique — keep or delete as preferred (they're gitignored only if .impeccable is; scripts/ is not).

## Questions to Consider

- Should the collapsed trace in reloaded history auto-expand its most recent turn, so reopening a session shows proof of work before requiring a click?
- What if the sidebar conversation rows revealed their actions as a hover *menu* (⋯) instead of three bare glyphs — one affordance, labeled items, no jargon?
- The composer is now state-aware (amber/active). What would make it the visual anchor — a subtle elevation change when streaming, so the eye finds status without reading any text?
