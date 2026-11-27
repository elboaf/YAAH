# ADR 0025: The composer engine is Lexical

Status: Accepted — decided in the #711 grilling (2026-11-07); implemented under #711.

## Context

The composer shipped skill chips as a canvas-measured paint over the
textarea (#105). The text stays literal and a `pointer-events:none`
overlay draws indigo pills over measured `$name` rects. That bought
safety (the model receives text verbatim) but built in a ceiling:

- Chips are paint, not objects: no hover delete, no click-to-inspect, no focus.
- The canvas wrap-walk hand-copies browser line breaking. Drift is possible.
- No path to non-text inline objects: @mentions, chat cards, forwarding.

The composer engine moves to Lexical (@lexical/react 0.53.0; peer React
>= 18; the repo runs React 19.3, verified). Discord's own composer runs
on Lexical; mentions and autocomplete ship as maintained plugins.
Evaluated and passed over: a better overlay measurement (keeps the
ceiling), a hand-rolled contenteditable (moves selection and IME
ownership in-house with unmaintained React bindings), Slate (more
assembly, same shape), Tiptap/ProseMirror (schema weight unjustified
without a formatting toolbar).

This is the load-bearing, hard-to-reverse decision of #711, so it is
recorded here before the code lands.

## Decision

The composer engine is Lexical. The editor holds **plain text as the
single source of truth in phase 1**: it serializes to the same plain
text YAAH sends today. `deriveInvokedSkills` (src/skillTokens.ts) keeps
scanning that text; the wire format (text + skills[], store.ts) is
untouched. `skillTokens.ts` survives as the send-side scanner. Only the
rendering side is replaced.

New invariant, tested both ways:

- chip inserted in the editor -> serialized text carries `$name` ->
  scanner finds it -> send path invokes it once.
- raw text edited -> chips re-derive from it.

No separate chip state.

### The composer is a bounded seam, not a wholesale rewrite

The composer lives inline inside the large `ChatPanel` component in
`src/components.tsx` (dictation/PTT insertion, draft persistence,
queued/steered sends, nativeMenu, auto-grow all intertwined). Rather
than rip the whole thing out in one commit, the engine moves behind a
**single component boundary**: a Lexical editor + a skill-chip inline
node + `$`/`/` typeahead, standing in for the textarea. The retires-at-
that-boundary contract means each phase is independently verifiable and
safe, and the change is reversible by deleting that boundary.

Phase 1 skeleton is built behind this boundary first; the textarea
retires there. Touchy paths (dictation, draft restore, queued/steered
sends, native menu, auto-grow) are ported one at a time in later
phases.

### Chip node semantics

Skill chips are atomic inline nodes that delete as a unit (one
backspace removes the whole chip). Hover/click semantics are defined
but not necessarily fully wired in phase 1:

- **Delete**: the trailing backspace removes the chip atomically.
- **Inspect/click**: a click places the caret inside the chip; a second
  click or Escape leaves it. (Full delete-on-click is a later polish.)

### The overlay is deleted, not kept

The canvas mirror (`tokenRects` state, canvas measurement effect,
`skillOverlayCanvasRef`, the `pointer-events:none` overlay render) is
**deleted** once the chip node renders. Zero canvas measurement code
left in the tree. The overlay was the safety harness for the old
paint-over approach; the real node supersedes it.

### Send path untouched, backend untouched

`deriveInvokedSkills` keeps scanning serialized plain text. The backend
(`test_skill_invocation.py`) is not edited and stays green; model
context is byte-identical for identical composer text. No wire-format
change.

### @keywords are out of scope

Discord-style @keywords semantics need harness-level wiring (hooks so
other chats can start from a mention). A later wayfinder session maps
that; not designed here. Formatting toolbar (bold/italic/code) is out
of scope. No backend or wire-format change.

## Considered and rejected

- **Better overlay measurement** — rejected: it keeps the ceiling (paint,
  not objects) and adds no real capability. The ceiling is the reason
  for #711.
- **Hand-rolled contenteditable** — rejected: moves selection and IME
  ownership in-house with unmaintained React bindings. Lexical owns
  that surface.
- **Slate** — rejected: more assembly, same shape.
- **Tiptap/ProseMirror** — rejected: schema weight unjustified without a
  formatting toolbar (out of scope).
- **Two sources of truth (chip state + text)** — rejected: the invariant
  "no separate chip state" keeps the send path trivially correct.

## Consequences

- **Bundle cost**: `@lexical/*` add weight. It is measured and stated
  in the PR description (phase 5).
- **Test setup**: Lexical under vitest/jsdom needs its own test
  utilities; a new dev setup is required.
- **contenteditable contract**: the right-click native menu contract
  (nativeMenu.tsx) may change — the editor owns the node tree, not a
  textarea.
- **Dictation/PTT**: insertion paths written for a textarea now insert
  into a different selection model; each path is ported carefully.
- **Auto-grow**: the editor replaces auto-grow height CSS assumptions
  with editor auto-height.
- **Reversibility**: the change is a single component boundary; deleting
  that boundary reverts to the textarea + overlay. The ADR status
  reflects that this is deliberate and hard-to-reverse-enough to record.

## Bundle measurement (phase 5)

Measured with `vite build` at the commit just before the overhaul landed
(before-lexical, 95a5cc86) vs. the current tree:

|                 | before-lexical | after-lexical | delta   |
| --------------- | -------------: | ------------: | ------: |
| Main JS bundle  |      706.82 kB |    962.92 kB | +256.1 kB |
| JS gzip         |      207.01 kB |    287.63 kB |  +80.6 kB |
| CSS             |       68.23 kB |     69.64 kB |  +1.4 kB |

The composer overhaul adds ~256 kB raw (about +81 kB gzip, +39%) to the
main JS bundle. This is the expected cost of shipping the `@lexical/*`
libraries. The bundle is a single ~960 kB chunk that already exceeds the
500 kB chunk-size warning; code-splitting the composer behind a
`dynamic import()` was out of scope for #711 (the ADR's "Consequences"
notes it as a possible future optimization).

## Revisit triggers

- A formatting need becomes real and consistent → reconsider Tiptap.
- @keywords wiring arrives → the chip node becomes a general inline-node
  framework; re-decide the node model.
- Lexical selection/IME regressions in dictation or IME input → revisit
  the editor choice or add a fallback.
