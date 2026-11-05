---
name: YAAH
description: Desktop AI coding agent — a mission-control console for watching and steering a working agent.
colors:
  console-bg: "#1e1e1e"
  console-deep: "#0d1117"
  surface: "#252525"
  surface-well: "#1b1b1b"
  hairline: "#333333"
  hairline-strong: "#444444"
  ink-bright: "#e6e6e6"
  ink-primary: "#d4d4d4"
  ink-secondary: "#a8a8a8"
  ink-muted: "#8a8a8a"
  ink-ghost: "#555555"
  terminal-green: "#58ff58"
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
  tool-delegate: "#e879f9"
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
    textColor: "{colors.ink-primary}"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
  input-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink-bright}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  tool-chip:
    backgroundColor: "rgba(13,17,23,0.7)"
    textColor: "{colors.terminal-green}"
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
    textColor: "{colors.terminal-green}"
    rounded: "{rounded.sm}"
  agent-card:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.hairline}"
    textColor: "{colors.ink-primary}"
    rounded: "{rounded.lg}"
    padding: "12px"
  composer:
    backgroundColor: "rgba(37,37,37,0.5)"
    borderColor: "{colors.hairline}"
    rounded: "{rounded.lg}"
  modal:
    backgroundColor: "{colors.console-bg}"
    textColor: "{colors.ink-primary}"
    rounded: "{rounded.lg}"
---

# Design System: YAAH

## Overview

**Creative North Star: "Mission Control, Terminal as Accent"**

YAAH's interface is a dark instrument panel for supervising a machine at work. Every agent action is a readable signal: tool calls arrive as green mono chips, state is a status light, and the human's own actions are the single strong blue voice in the room. The aesthetic is a terminal that grew awareness — dense, mono-led, unhurried — not a consumer chat app and not a marketing surface.

The surface world is warm neutral (#1e1e1e console, #252525 panels, #333 hairlines — the "retro accent" direction picked at design review and shipped by issue #643's full migration). Machine output carries the identity: phosphor green (#58ff58) mono on deep well blocks (#0d1117). The green is an accent, not a theme — it marks *where the machine is speaking*, never where a human can act.

Density is high but never ambiguous: compact rows (6–8px vertical padding), 10–12px secondary text, and hairline borders do the separating, so color is freed to carry meaning only. Machine output is monospace and green; human prose is the only place the sans voice breathes, in neutral ink. Motion exists solely to report state — a chip sliding in, a pulse while a tool runs — never to decorate.

**Key Characteristics:**
- Warm-neutral gray surfaces (#1e1e1e / #252525 / #333); color is information, not decoration
- Monospace is the voice of the machine — and the machine speaks green (#58ff58)
- Sans in neutral ink is the voice of the human
- Flat surfaces separated by 1px hairlines, never shadows
- Shadows reserved for layers that float above the app (modals, menus, live question card)
- Compact, instrument-grade spacing throughout

## Colors

A warm-neutral console where phosphor green marks the machine's voice, one blue accent marks human intent, and a small set of state colors report what the machine is doing.

### Primary
- **Action Blue** (#2563eb): the only color a human action may use — New chat, Send, Save, and focus rings on the composer. Hover lifts to #3b82f6. If blue appears, the user did it or is about to.

### Terminal Accent
- **Terminal Green** (#58ff58, optional glow rgba(88,255,88,0.3)): the machine's voice — tool chips (running *and* done), the telemetry tape, trace lines, code blocks, diff context lines. Set in JetBrains Mono on deep well surfaces (#0d1117). The `.terminal-text` / `.crt-glow` classes in index.css are its only source. Green is never a button, never a human-action color, and never decorates non-machine content.

### State
- **Working Amber** (#fbbf24): the machine-at-work signal — pulsing status dot, running-tool pulse. Never a button.
- **Ask Orange** (#f97316, deep #c2410c): the agent-is-asking channel — question card border, option selection, the pulsing `?`. Distinct from amber so "busy" and "waiting on you" never blur.

### Tool Spectrum
- **Tool Spectrum** (read #38bdf8 · search #a78bfa · shell #34d399 · web #22d3ee · image #f472b6 · delegate #e879f9 · edit #fbbf24): per-tool glyph identity inside chips and traces. Sanctioned to appear anywhere a tool is named — not elsewhere. Delegate (fuchsia) marks sub-agent delegation (spawn_agent) and its nested transcript blocks. The glyph carries the spectrum; the chip text itself is green.

### Neutral (the surface + ink system)
- **Console Background** (#1e1e1e): app root (`bg-[#1e1e1e]` on the App shell).
- **Console Deep** (#0d1117): machine wells — code blocks, diffs, tool chip fills, the telemetry tape. The floor of the system, never pure black.
- **Surface** (#252525): panels, agent message cards, inputs, chip bases. Implemented as Tailwind zinc-800 (retuned; see below).
- **Surface Well** (#1b1b1b): the sidebar and header strips — darker than panels so the chat column reads forward.
- **Hairline** (#333333): the default 1px separator and card border.
- **Hairline Strong** (#444444): borders that must read on filled surfaces (composer textarea, hover states).
- **Ink Bright** (#e6e6e6): emphasized text on any surface.
- **Ink Primary** (#d4d4d4): prose and secondary text.
- **Ink Secondary** (#a8a8a8): quiet text, chip targets.
- **Ink Muted** (#8a8a8a): labels, placeholders, the "agent" card label.
- **Ink Ghost** (#555555): timestamps, line numbers, glyphs, disabled hints.
- **Skill Indigo** (#312e81 at 60% with #c7d2fe text): skill chips only — the one cool violet family reserved for the skills system.

**Implementation note (issue #643):** the zinc ladder in `tailwind.config.js` is deliberately retuned so the *names* keep their roles: zinc-950→#0d1117, zinc-900→#1b1b1b, zinc-800→#252525, zinc-700→#333333, zinc-600→#555555 (faint ink, not borders), zinc-500→#8a8a8a, zinc-400→#a8a8a8, zinc-300→#d4d4d4, zinc-200→#e6e6e6. Use the zinc utility that matches the ROLE above; do not introduce raw hex for surfaces outside the composer/user-bubble tints.

### Named Rules
**The Color Is State Rule.** Color never decorates; it reports. Blue = human action, green = machine voice, amber = machine working, orange = agent asking, emerald/red = done/error. Tool glyph colors are the single sanctioned exception, and only where a tool is named.

**The One Voice Rule.** Action Blue is the only accent permitted on interactive controls. State colors may appear on indicators, never on buttons.

**The Machine Speaks Green Rule.** Anything the machine produced — tool names, tape, traces, code, diff context — is monospace in #58ff58 on a #0d1117 well. If a surface is green-mono, it is machine output; if it's neutral sans, it's human space.

## Typography

**Body Font:** ui-sans-serif, system-ui, sans-serif (human prose, labels, buttons)
**Mono Font:** JetBrains Mono, with Consolas, monospace fallback (all machine output)

**Character:** A two-voice pairing — the sans voice is plain and quiet so the mono voice reads as the machine speaking. There is no display face; hierarchy is size and case, not family contrast.

### Hierarchy
- **Title** (600, 14px, 1.625): panel headings ("FILES", "Settings").
- **Body** (400, 14px, 1.625): user and agent prose. Agent text runs the full chat-column width; user bubbles cap at 85%.
- **Secondary** (400, 12px, 1.5): option descriptions, settings help, conversation rows.
- **Mono Body** (400, 11px/16px): tool chips, traces, code, args/results — green on wells.
- **Label** (500, 10px, 0.1em tracking, uppercase): section markers — "agent" on message cards, "agent asks — pick an answer", the status line.

### Named Rules
**The Machine Speaks Mono Rule.** Anything the machine produced is monospace. Sans is reserved for human language: prose, questions, button labels.

## Layout

A fixed two-pane console: sidebar (256px, min 220px, #1b1b1b with a #333 right hairline) for identity, workspace, model, and conversations; chat takes the remainder with a min-width-0 flex so long content truncates instead of pushing panes out. Workspace groups toggle by name click — click expands the group and starts a new draft chat there, click again collapses (persisted per workspace); an expanded group shows its 5 most recent chats plus the open conversation, with a dimmed "show more" stepping +5 per click (session-only). Chat content sits in a 16px-padded scroll column with 16px between messages. The composer is a bordered #252525 rounded-lg card pinned below the transcript, with the status line (6px dot + 10px mono label) as the last row of the app. Spacing rhythm is a tight 4/8/12/16px scale; vertical padding inside controls is 4–8px.

**Agent messages render as cards:** each agent turn is a #252525 panel with a 1px #333 border, 8px radius, 12px padding, and an uppercase mono "agent" label (10px, #8a8a8a, 0.1em tracking) above the body. The planning fold shares the card treatment with a sky label.

**User messages render as blue-tinted bubbles:** rgba(74,144,217,0.08) fill, 1px rgba(37,99,235,0.45) border, 8px radius, right-aligned, capped at 85% width — the one place blue tints a surface, because the human wrote it. Queued (not yet landed) bubbles keep the neutral #252525 fill with a dashed #555555 border.

**Files panel — removed from the GUI (deliberate).** The workspace file tree and its collapsed 28px "FILES" rail are absent from the interface, but the functional code remains in components.tsx (`FilesPanel`, tree fetch/lazy-load/cache, context menu) and the file-preview popup it fed is still wired. Restore by re-adding `<FilesPanel />` to App.tsx. Tree indent stays 12px per level for when it returns.

## Elevation & Depth

Flat by doctrine: surfaces are separated with 1px hairlines (#333333, or #444444 where a border must read on a filled surface), never shadows. Depth is conveyed tonally (surface #252525 on console #1e1e1e, machine wells #0d1117, sidebar well #1b1b1b) and by two sanctioned devices: the live tool ticker's right-edge gradient fade mask, and floating layers.

### Shadow Vocabulary
- **Live question card** (`0 10px 15px -3px rgba(0,0,0,0.1), 0 4px 6px -4px rgba(0,0,0,0.1)`): the ask_user card, because it is an interruption floating above the composer.
- **Context menu** (`0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1)`): the file-tree menu.
- **Modal** (`0 25px 50px -12px rgba(0,0,0,0.25)` over a black/60 overlay): preview and settings dialogs.

### Named Rules
**The Floating Layer Rule.** Shadows belong only to layers that float above the app — modals, menus, the live question card. In-flow surfaces never carry a shadow.

## Shapes

Precise radii on the hairline grid: controls and chips use 4px; message cards, the composer, user bubbles, and modals use 8px (`rounded-lg`); the only circles are semantic — the 6px status dot and the 16px image-remove button. Borders are 1px everywhere; there are no thick outlines. The dashed border means exactly two things: the queued-message bubble and the composer's active drag-over drop target (plus the "Something else…" free-text affordance on the question card).

## Components

### Buttons
- **Shape:** 4px radius, compact padding (6px 12px).
- **Primary:** Action Blue (#2563eb) fill, white text, 14px sans — Send, New chat, Save. Hover #3b82f6; disabled 50% opacity.
- **Secondary / Ghost:** transparent on console, 1px #444444 border, #d4d4d4 text, 12px; hover fills #2e2e2e.
- **Destructive:** 1px #b91c1c border with #fca5a5 text (Stop) or borderless red text (remove/delete); hover deepens toward #450a0a fills.
- **Micro text buttons** (10px, #8a8a8a): refresh, export, sys — quiet utilities that brighten to #d4d4d4 on hover.

### Chips
- **Tool chip:** 11px mono, 2px 6px, 4px radius; green (#58ff58) text in both states. Done = rgba(13,17,23,0.7) fill; running = rgba(63,63,70,0.6) fill with a pulsing amber dot. Glyph carries the tool's spectrum color.
- **Attachment chip:** 10px mono on #252525, #d4d4d4 text, × remove.
- **Skill chip:** rgba(49,46,129,0.6) with #c7d2fe 10px mono text — the indigo family appears nowhere else.

### Cards / Containers
- **Agent message card:** #252525, 1px #333 border, 8px radius, 12px padding, uppercase mono "agent" label; emission prose inside in neutral sans ink.
- **User bubble:** rgba(74,144,217,0.08) fill, 1px rgba(37,99,235,0.45) border, 8px radius; text #e6eef7.
- **Code block:** #0d1117 body, 1px #333 border, 4px radius; header strip #1b1b1b with 10px mono language tag and copy button, separated by a #252525 hairline. Code text green; line numbers in #555555, gutter right-aligned; syntax colors from codeview.ts ride on top.
- **Diff rows:** additions rgba(6,78,59,0.6) with #6ee7b7 text, deletions rgba(69,10,10,0.6) with #fca5a5 text, context lines green; `+`/`-` glyphs at 60% opacity.
- **Ask card:** #1b1b1b, 1px rgba(194,65,12,0.6) border, 8px radius, 12px padding, live-question shadow; selected option fills rgba(67,20,7,0.4) with #fed7aa text and #f97316 border.
- **Modals:** #1e1e1e, 1px #444444 border, 8px radius, floating-layer shadow over a black/60 scrim.

### Inputs / Fields
- **Composer:** rgba(37,37,37,0.5) fill in a bordered 8px-radius card; 1px #333 border at rest, shifting to #3b82f6 on focus-within or blue dashed on drag-over. The textarea itself is transparent; the card owns the border.
- **Other fields:** #252525 fill, 1px #333 border, 4px radius, 8px 12px padding; mono 12px for paths/models, sans 14px for prose.
- **Focus:** border shifts to #3b82f6 (composer) or #f97316 (inside the ask card) — a border change only, never a glow.
- **Error:** inline 11px red text below the field; no red border treatment.

### Navigation
- **Conversation rows:** 12px text, 4px 8px padding, 4px radius; active = #252525 fill with #f4f4f5 text; inactive hover = a soft #252525 fill. Status colors remain visible in their dedicated indicators. Row actions (md↓, sys, ×) are 10px ghost buttons revealed on hover.
- **File tree:** 11px rows, 12px-per-level indent, caret/• glyph in #8a8a8a, hover fill #252525, context menu on right-click.

### Signature: the Tool Ticker
The live turn renders recent tool calls as a single horizontal chip row of green mono chips, newest sliding in from the left (0.25s ease-out, 24px travel) and older chips fading right under a gradient mask — the row never grows past the panel. Beneath it, the telemetry tape streams the session's raw tool output as a green mono line. When the turn ends, the ticker collapses into one expandable trace line ("N calls · glyph name ×count", green mono) whose rows expand to per-call detail. This ticker→trace collapse is the system's signature behavior: loud while working, silent when done.

### Status Line
The app's pulse: a 6px dot (amber pulsing = working, red = error, emerald = idle) beside a 10px mono label, last row of the chat panel.

## Do's and Don'ts

### Do:
- **Do** separate in-flow surfaces with 1px hairlines (#333333; #444444 ONLY on borders of inputs/fields filled with #252525, where the border must read against the raised fill). #444444 is never the default separator.
- **Do** set machine output in JetBrains Mono at 10–11px, green (#58ff58) on #0d1117 wells; labels uppercase with 0.1em tracking.
- **Do** keep Action Blue (#2563eb) exclusively on human-initiated controls.
- **Do** report state with the status colors: amber working, orange asking, emerald done, red error.
- **Do** keep controls compact: 4px radius on chips/controls, 8px on cards and the composer, 6–8px vertical padding, 10–12px secondary text.
- **Do** pick zinc utilities by ROLE (see the retuned ladder above) — zinc-800 means "panel", zinc-700 means "hairline", zinc-600 means "faint ink".

### Don't:
- **Don't** put shadows on in-flow surfaces — shadows are for modals, menus, and the ask card only.
- **Don't** use pure black (#000) as a surface; the floor is #0d1117, and only for machine wells.
- **Don't** introduce new accent hues; the tool spectrum colors appear only where a tool is named, indigo only for skills.
- **Don't** spend terminal green on anything but machine voice — no green buttons, green links, or green human prose. If a human can click it, it's blue or ghost zinc.
- **Don't** animate for delight — motion is chip-in (0.25s ease-out) and run-pulse (1s ease-in-out) or nothing; no bounce or elastic easing.
- **Don't** render a button in a state color (amber/orange/emerald/red); state colors are indicators, not actions. Human-initiated buttons are blue or ghost zinc.
- **Don't** spend amber on anything but progress/working (status dot, ticker, run pulse, slow-turn, Steer). Approval cards are ORANGE (an approval is the agent asking); queued chips, git drift, cached-offline labels, warnings, and capture affordances are neutral zinc ink — only their glyph may carry color when it must read.
- **Don't** give machine-config inputs a blue focus border; their focus neutralizes to zinc-500. Blue focus is the composer's and other named human-action surfaces' alone.
- **Don't** change the composer border to signal streaming — the status dot and ticker own that.
- **Don't** reintroduce raw zinc hex values from the old palette (#18181b, #27272a, #3f3f46) — they are dead tokens; the retuned ladder in tailwind.config.js is the source of truth.
