---
name: YAAH
description: Desktop AI coding agent — a mission-control console for watching and steering a working agent.
colors:
  console-bg: "#18181b"
  console-deep: "#09090b"
  surface-raised: "#27272a"
  hairline-soft: "#27272a"
  hairline-strong: "#3f3f46"
  ink-primary: "#f4f4f5"
  ink-secondary: "#d4d4d8"
  ink-muted: "#a1a1aa"
  ink-faint: "#71717a"
  ink-ghost: "#52525b"
  action-blue: "#2563eb"
  action-blue-hover: "#3b82f6"
  working-amber: "#fbbf24"
  ask-orange: "#f97316"
  ask-orange-deep: "#c2410c"
  done-emerald: "#059669"
  error-red: "#ef4444"
  skill-indigo: "#312e81"
  skill-indigo-text: "#c7d2fe"
  tool-read: "#38bdf8"
  tool-search: "#a78bfa"
  tool-shell: "#34d399"
  tool-web: "#22d3ee"
  tool-image: "#f472b6"
typography:
  label:
    fontFamily: "JetBrains Mono, Consolas, monospace"
    fontSize: "10px"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "0.1em"
  mono-body:
    fontFamily: "JetBrains Mono, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: "16px"
  body:
    fontFamily: "ui-sans-serif, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.625
  title:
    fontFamily: "ui-sans-serif, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 600
    lineHeight: 1.4
rounded:
  sm: "4px"
  lg: "8px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
components:
  button-primary:
    backgroundColor: "{colors.action-blue}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
  input-field:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.ink-primary}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  tool-chip:
    backgroundColor: "rgba(39,39,42,0.7)"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.sm}"
    padding: "2px 6px"
  skill-chip:
    backgroundColor: "rgba(49,46,129,0.6)"
    textColor: "{colors.skill-indigo-text}"
    rounded: "{rounded.sm}"
    padding: "2px 8px"
  nav-item-active:
    backgroundColor: "{colors.action-blue}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "6px 8px"
  code-block:
    backgroundColor: "{colors.console-deep}"
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.sm}"
  modal:
    backgroundColor: "{colors.console-bg}"
    textColor: "{colors.ink-primary}"
    rounded: "{rounded.lg}"
---

# Design System: YAAH

## Overview

**Creative North Star: "Mission Control"**

YAAH's interface is a dark instrument panel for supervising a machine at work. Every agent action is a readable signal: tool calls arrive as labeled chips with colored glyphs, state is a status light, and the human's own actions are the single strong blue voice in the room. The aesthetic is a terminal that grew awareness — dense, mono-led, unhurried — not a consumer chat app and not a marketing surface.

Density is high but never ambiguous: compact rows (6–8px vertical padding), 10–12px secondary text, and hairline zinc borders do the separating, so color is freed to carry meaning only. Machine output is monospace; human prose is the only place the sans voice breathes. Motion exists solely to report state — a chip sliding in, a pulse while a tool runs — never to decorate.

**Key Characteristics:**
- Near-monochrome zinc world; color is information, not decoration
- Monospace is the voice of the machine; sans is the voice of the human
- Flat surfaces separated by 1px hairlines, never shadows
- Shadows reserved for layers that float above the app (modals, menus, live question card)
- Compact, instrument-grade spacing throughout

## Colors

A near-monochrome zinc console where one blue accent marks human intent and a small set of state colors report what the machine is doing.

### Primary
- **Action Blue** (#2563eb): the only color a human action may use — New chat, Send, Save, the active conversation row, focus rings on the composer. Hover lifts to #3b82f6. If blue appears, the user did it or is about to.

### Secondary
- **Working Amber** (#fbbf24): the machine-at-work signal — pulsing status dot, running-tool glyph. Never a button.
- **Ask Orange** (#f97316, deep #c2410c): the agent-is-asking channel — question card border, option selection, the pulsing `?`. Distinct from amber so "busy" and "waiting on you" never blur.

### Tertiary
- **Tool Spectrum** (read #38bdf8 · search #a78bfa · shell #34d399 · web #22d3ee · image #f472b6 · edit #fbbf24): per-tool glyph identity inside chips and traces. Sanctioned to appear anywhere a tool is named — not elsewhere.

### Neutral
- **Console Background** (#18181b): app shell and panels.
- **Console Deep** (#09090b): code blocks and diff surfaces — the floor of the system, never pure black.
- **Raised Surface** (#27272a): inputs, chips, hover fills; also doubles as the panel hairline.
- **Strong Hairline** (#3f3f46): component borders that must read against raised surfaces.
- **Ink Primary** (#f4f4f5): primary text on any surface.
- **Ink Secondary** (#d4d4d8): prose and secondary text.
- **Ink Muted** (#a1a1aa): machine metadata, done-tool chips.
- **Ink Faint** (#71717a): labels, placeholders, targets.
- **Ink Ghost** (#52525b): line numbers, disabled hints, the status line.
- **Skill Indigo** (#312e81 at 60% with #c7d2fe text): skill chips only — the one cool violet family reserved for the skills system.

### Named Rules
**The Color Is State Rule.** Color never decorates; it reports. Blue = human action, amber = machine working, orange = agent asking, emerald/red = done/error. Tool glyph colors are the single sanctioned exception, and only where a tool is named.

**The One Voice Rule.** Action Blue is the only accent permitted on interactive controls. State colors may appear on indicators, never on buttons.

## Typography

**Body Font:** ui-sans-serif, system-ui, sans-serif (human prose, labels, buttons)
**Mono Font:** JetBrains Mono, with Consolas, monospace fallback (all machine output)

**Character:** A two-voice pairing — the sans voice is plain and quiet so the mono voice reads as the machine speaking. There is no display face; hierarchy is size and case, not family contrast.

### Hierarchy
- **Title** (600, 14px, 1.625): panel headings ("AI Coding Agent", "FILES", "Settings").
- **Body** (400, 14px, 1.625): user and agent prose, max ~70ch in the chat column.
- **Secondary** (400, 12px, 1.5): option descriptions, settings help, conversation rows.
- **Mono Body** (400, 11px/16px): tool chips, traces, file tree, code, args/results.
- **Label** (500, 10px, 0.1em tracking, uppercase): section markers — "agent", "agent asks — pick an answer", the status line.

### Named Rules
**The Machine Speaks Mono Rule.** Anything the machine produced — tool names, paths, commands, code, traces, status — is monospace. Sans is reserved for human language: prose, questions, button labels.

## Layout

A fixed three-pane console: sidebar (256px, min 220px) for identity, workspace, model, and conversations; files panel (240px, min 200px, collapsible to a 28px vertical rail) present only at ≥1280px (xl); chat takes the remainder with a min-width-0 flex so long content truncates instead of pushing panes out. Chat content sits in a 16px-padded scroll column with 16px between messages; the composer is a 12px-padded band pinned below it, with the status line (6px dot + 10px mono label) as the last row of the app. Spacing rhythm is a tight 4/8/12/16px scale; vertical padding inside controls is 4–8px. Depth of the file tree is 12px indent per level.

## Elevation & Depth

Flat by doctrine: surfaces are separated with 1px hairlines (#27272a, or #3f3f46 where a border must read on a raised surface), never shadows. Depth is conveyed tonally (raised #27272a on console #18181b, code on #09090b) and by two sanctioned devices: the live tool ticker's right-edge gradient fade mask, and floating layers.

### Shadow Vocabulary
- **Live question card** (`0 10px 15px -3px rgba(0,0,0,0.1), 0 4px 6px -4px rgba(0,0,0,0.1)`): the ask_user card, because it is an interruption floating above the composer.
- **Context menu** (`0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1)`): the file-tree menu.
- **Modal** (`0 25px 50px -12px rgba(0,0,0,0.25)` over a black/60 overlay): preview and settings dialogs.

### Named Rules
**The Floating Layer Rule.** Shadows belong only to layers that float above the app — modals, menus, the live question card. In-flow surfaces never carry a shadow.

## Shapes

Small, precise radii on a hairline grid: controls and chips use 4px, floating cards and modals use 8px, and the only circles are semantic — the 6px status dot and the 16px image-remove button. Borders are 1px everywhere; there are no thick outlines, and the dashed border is reserved for one meaning: the "Something else…" free-text affordance on the question card.

## Components

### Buttons
- **Shape:** 4px radius, compact padding (6px 12px).
- **Primary:** Action Blue (#2563eb) fill, white text, 14px sans — Send, New chat, Save. Hover #3b82f6; disabled 50% opacity.
- **Secondary / Ghost:** transparent on console, 1px #3f3f46 border, #d4d4d8 text, 12px; hover fills #27272a.
- **Destructive:** 1px #b91c1c border with #fca5a5 text (Stop) or borderless red text (remove/delete); hover deepens toward #450a0a fills.
- **Micro text buttons** (10px, #71717a): refresh, export, sys — quiet utilities that brighten to #d4d4d8 on hover.

### Chips
- **Tool chip:** 11px mono, 2px 6px, 4px radius; done = rgba(39,39,42,0.7) with #a1a1aa text, running = rgba(63,63,70,0.6) with #e4e4e7 text and a pulsing amber dot. Glyph carries the tool's spectrum color.
- **Attachment chip:** 10px mono on #27272a, #d4d4d8 text, × remove.
- **Skill chip:** rgba(49,46,129,0.6) with #c7d2fe 10px mono text — the indigo family appears nowhere else.

### Cards / Containers
- **Code block:** #09090b body, 1px #3f3f46 border, 4px radius; header strip #18181b with 10px mono language tag and copy button, separated by a #27272a hairline. Line numbers in #52525b, gutter right-aligned.
- **Diff rows:** additions rgba(6,78,59,0.6) with #6ee7b7 text, deletions rgba(69,10,10,0.6) with #fca5a5 text, `+`/`-` glyphs at 60% opacity.
- **Ask card:** #18181b, 1px rgba(194,65,12,0.6) border, 8px radius, 12px padding, live-question shadow; selected option fills rgba(67,20,7,0.4) with #fed7aa text and #f97316 border.
- **Modals:** #18181b, 1px #3f3f46 border, 8px radius, floating-layer shadow over a black/60 scrim.

### Inputs / Fields
- **Style:** #27272a fill, 1px #3f3f46 border, 4px radius, 8px 12px padding; mono 12px for paths/models, sans 14px for prose.
- **Focus:** border shifts to #3b82f6 (composer) or #f97316 (inside the ask card) — a border change only, never a glow.
- **Error:** inline 11px red text below the field; no red border treatment.

### Navigation
- **Conversation rows:** 12px text, 4px 8px padding, 4px radius; active = #2563eb fill with white text; hover #27272a. Row actions (md↓, sys, ×) are 10px ghost buttons revealed on hover.
- **File tree:** 11px rows, 12px-per-level indent, caret/• glyph in #71717a, hover fill #27272a, context menu on right-click.

### Signature: the Tool Ticker
The live turn renders recent tool calls as a single horizontal chip row, newest sliding in from the left (0.25s ease-out, 24px travel) and older chips fading right under a gradient mask — the row never grows past the panel. When the turn ends, the ticker collapses into one expandable trace line ("N calls · glyph name ×count"). This ticker→trace collapse is the system's signature behavior: loud while working, silent when done.

### Status Line
The app's pulse: a 6px dot (amber pulsing = working, red = error, emerald = idle) beside a 10px mono label, last row of the chat panel.

## Do's and Don'ts

### Do:
- **Do** separate in-flow surfaces with 1px hairlines (#27272a; #3f3f46 on raised fills).
- **Do** set machine output in JetBrains Mono at 10–11px; labels uppercase with 0.1em tracking.
- **Do** keep Action Blue (#2563eb) exclusively on human-initiated controls.
- **Do** report state with the status colors: amber working, orange asking, emerald done, red error.
- **Do** keep controls compact: 4px radius, 6–8px vertical padding, 10–12px secondary text.

### Don't:
- **Don't** put shadows on in-flow surfaces — shadows are for modals, menus, and the ask card only.
- **Don't** use pure black (#000) as a surface; the floor is #09090b, and only for code.
- **Don't** introduce new accent hues; the tool spectrum colors appear only where a tool is named, indigo only for skills.
- **Don't** animate for delight — motion is chip-in (0.25s ease-out) and run-pulse (1s ease-in-out) or nothing; no bounce or elastic easing.
- **Don't** render a button in a state color (amber/orange/emerald/red); state colors are indicators, not actions.
