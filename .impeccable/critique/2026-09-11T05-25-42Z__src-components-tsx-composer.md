---
target: Composer (src/components.tsx)
total_score: 19
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
target_identity: "file:C:\\Users\\Administrator\\YAAH\\src\\components.tsx:Composer"
timestamp: 2026-09-11T05-25-42Z
slug: src-components-tsx-composer
---
# Critique: Composer (src/components.tsx — Composer component)

Method: DEGRADED — single-context (no sub-agent/Task tool exposed in this session; sequential A→B fallback per critique flow)

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | Attachment drops are silent; composer shows no streaming state |
| 2 | Match System / Real World | 2 | "/s" convention unexplained; Enter/Tab behave unlike any other input |
| 3 | User Control and Freedom | 2 | No way to type a literal "/s …" message; cleared draft unrecoverable |
| 4 | Consistency and Standards | 2 | Enter/Tab hijacked while menu open; emoji 📎 vs glyph icon language; IME Enter bug |
| 5 | Error Prevention | 2 | Caps exist (5MB/200KB/4 images) but every violation is silent |
| 6 | Recognition Rather Than Recall | 2 | /s menu is good once found; nothing surfaces it |
| 7 | Flexibility and Efficiency | 3 | Enter/Shift+Enter, arrows, Tab-complete, drag/paste/attach — strong once learned |
| 8 | Aesthetic and Minimalist Design | 3 | On-system; placeholder is a 130-character instruction dump |
| 9 | Error Recovery | 1 | Failed send empties the composer; banner has no retry/dismiss |
| 10 | Help and Documentation | 1 | Keybinding docs live in a placeholder that vanishes on first keystroke |
| **Total** | | **19/40** | **Needs work — the failure paths drag a strong core down** |

Note: this is a focused re-critique of the composer only, so the score is not comparable to the 24/40 whole-UI run — the composer concentrates the app's worst failure paths (recovery 1, help 1).

## Design Specificity Verdict

**Mechanism authored, presentation generic.** The `/s` chip pipeline (menu → chip → invoked-skills injection) and the attachments-as-fenced-blocks design are YAAH's own; no generic chat composer does this. But none of that authorship is visible: the menu hides behind a magic string, the attachment pipeline fails silently, and the composer shell (textarea + 📎 + Send) is category-standard. The signature ticker→trace behavior lives one panel above; the composer itself reads as stock.

**Deterministic scan** (`impeccable detect --json src/components.tsx`): 2 findings, 1 in scope.
- `gray-on-color` (warning) at components.tsx:1554 — **in scope, false positive**: the image-thumb remove button pairs `text-zinc-300` with `bg-zinc-700` at rest and `text-white` with `bg-red-600` on hover; the detector's static pairing never renders. (Rest contrast ≈ 6:1, passes AA even at 10px.)
- `design-system-color` (advisory) at components.tsx:316 — ticker fade mask `rgba(0,0,0,0.35)`; out of composer scope (ToolTicker), already flagged in the full-UI run.

**Visual overlays**: not available — no browser automation tool exposed this session; CLI scan is the machine evidence.

## Overall Impression

The composer is the most-touched surface in the app and the least-considered: its happy path is genuinely good (three input modalities, keyboard-complete skill menu), and every failure path is either silent or destructive. The single biggest opportunity: the composer should survive failure — a failed send must not cost the user their prompt.

## What's Working

1. **Three input modalities, one pipeline** — drag, paste, and the attach button all converge on the same staged-chips model, and text files are inlined as fenced blocks with a NUL-byte binary check. That's a thoughtful, authored mechanism.
2. **The skill menu's keyboard contract** — arrows, Enter/Tab to pick, Esc to dismiss, mouseEnter sync, mousedown-with-preventDefault so the textarea keeps focus. Someone cared.
3. **Stop does the full job** — client abort AND server-side cancel; the `[stopped]` marker lands in the transcript. Control is real, not cosmetic.

## Priority Issues

1. **[P1] A failed send destroys the draft** — `send()` clears `input`, `attachments`, `images`, and `pickedSkills` synchronously, *before* the network call; on error the text exists only in the message log and the composer is empty.
   **Why it matters**: the most common failure (provider down, bad key, network blip) costs the user their entire composed prompt — the exact abandonment moment, and a trust failure for a transparency-first product.
   **Fix**: clear state only after the stream opens (or on `done`); on error, restore input/attachments and show the failure inline under the composer.
   **Suggested command**: `/impeccable harden`

2. **[P1] Enter and Tab are hijacked whenever the `/s` menu is open** — with matches present, Enter *always* picks a skill and Tab always completes; typing a literal message that starts with `/s` is impossible, and a user typing "/s" + Enter gets the alphabetically-first skill with no confirmation.
   **Why it matters**: input interception that can't be escaped reads as a bug; silent first-item commitment can invoke a skill the user didn't choose.
   **Fix**: Enter picks only on an explicit selection (arrowed highlight or non-empty query); Tab completes; Esc-then-Enter always sends; document the escape in the menu footer.
   **Suggested command**: `/impeccable polish`

3. **[P1] Every attachment violation is silent** — non-images, >5MB images, the 5th image, >200KB text files, NUL-containing binaries, and unreadable files all vanish without a word; the user believes the agent received them.
   **Why it matters**: the agent then works from incomplete context; the user discovers it only when the answer ignores their file. Silent data loss in the product whose thesis is "you can see everything."
   **Fix**: stage-then-reject with an inline chip: "screenshot.png skipped — 7.2 MB exceeds the 5 MB limit"; show the caps before they're hit.
   **Suggested command**: `/impeccable harden`

4. **[P2] The composer never reflects the agent's state** — while streaming, the textarea stays editable, Send just disables, Stop appears and shifts the row, and the only working indicator is the status line at the panel's bottom edge.
   **Why it matters**: the composer is where the user's eyes are; state shown 400px away might as well be hidden. The layout shift reads as jitter on every turn.
   **Fix**: swap Send→Stop in place (no reflow), tint the composer border amber while streaming, and auto-grow the textarea (rows=2 → ~8) instead of a fixed scroll box.
   **Suggested command**: `/impeccable polish`

5. **[P2] IME composition breaks Enter-to-send** — `onKeyDown` sends on Enter without checking `e.nativeEvent.isComposing`, so CJK input-method users' composition-confirm Enter fires the message mid-composition.
   **Why it matters**: for a public-distribution product this is a functional i18n bug for a large user population, and it corrupts the prompt.
   **Fix**: guard both the skill-menu and send Enter handlers with `isComposing`.
   **Suggested command**: `/impeccable harden`

## Persona Red Flags

**Alex (Power User)**: Lives in this box — and hits the Enter hijack the first time they try to send a message that starts with "/s"; no auto-grow means composing a 20-line prompt in a 2-row window; no way to see attachment sizes before send; drag-leave flicker when crossing child elements. Will build workarounds, then resentfully ask for shortcuts.

**Jordan (First-Timer)**: Never discovers `/s` (placeholder never mentions it); drops a file onto the chat log — outside the composer's drop zone — and the browser may navigate away from the app entirely; pastes an image, sees nothing appear (over the 5MB cap), concludes paste is broken. Abandons at first confusion.

**Supervisor Sam** (transparency-first, from PRODUCT.md): stages a file, sends, and can't verify what the agent actually received — the "[1 image attached]" note and fenced blocks are in the *message text*, but rejected files left no trace. For an audit-minded user, silent rejection is indistinguishable from silent success.

## Minor Observations

- The placeholder does five jobs (task prompt + 3 keybindings + attachment docs) and disappears on first keystroke; move keybinding docs to a persistent one-line hint row.
- 📎 is the only emoji in an otherwise disciplined glyph system (toolGlyphs use text symbols); an SVG paperclip matches the icon language.
- Image-remove × buttons (16px) have no aria-label and no visible focus state; the textarea has no accessible label.
- The skill menu is a visual list, not a listbox — no `role`, no `aria-activedescendant`, selection not announced to screen readers.
- `onDragLeave` flickers when crossing child elements (no counter); the drop zone should be the whole app surface with `preventDefault` on dragover at the window level.
- Skill chips show "/s name" while attachment chips show a bare filename — two chip grammars for the same staging row.
- The skill menu can overlay the attachment chips it relates to (absolute bottom-1 above the input row).

## Questions to Consider

- What if the composer were the app's anchor — border tinted amber while the agent works, so state is visible exactly where the user is looking?
- What if dropped files landed as *reviewable pending chips* (name, size, type, reject reason) instead of silently joining or vanishing?
- What if "/" opened a general command menu (skills, new chat, export, settings) instead of a magic "/s" string — discoverable by the convention every power user already knows?
