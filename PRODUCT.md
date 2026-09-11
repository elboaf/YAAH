# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Developers who want to run an AI coding agent as a local desktop app. YAAH is distributed publicly, so first-run experience, defaults, and docs are product requirements rather than afterthoughts. The core situation: a developer points YAAH at a project folder on their machine and drives coding tasks through a streaming agent conversation.

## Product Purpose

YAAH is a desktop AI coding agent. It gives a model real tools on the user's machine — shell (bash/powershell), file read/write/edit, search, web search/fetch, image viewing — and shows every tool call transparently: live while streaming, collapsed to an expandable trace afterwards. Success is a trustworthy agent loop: the user can see what the agent is doing, answer its questions inline (ask_user cards), stop it mid-turn, and inspect any file it touched.

## Positioning

Explicitly undecided — no deliberate differentiation has been chosen (user-confirmed). Candidates visible in the codebase, none committed: transparent local agent loop, skills-first extensibility (~/.yaah/skills), BYO-key multi-provider freedom. Future design and copy work must not invent a position; revisit with the user when positioning matters.

## Operating Context

- Runs as a Tauri 2 desktop app (Windows/macOS/Linux) with an embedded FastAPI backend subprocess; also runnable in a browser via `npm run dev` (Vite + uvicorn on localhost:8765).
- The user configures their own model providers (any OpenAI-compatible base URL + API key) in Settings; keys stay in local config.json and never reach the browser.
- The agent operates inside a user-chosen workspace folder; the file tree panel and preview modal are scoped to it.
- Skills are user-extensible markdown folders in ~/.yaah/skills, invoked via `/s` or chips; the model can also load them itself mid-turn.
- Conversations persist in a local SQLite database (backend/data/agent.db) and export as Markdown.

## Capabilities and Constraints

Confirmed functionality: streaming agent turns with live tool ticker and collapsed per-turn trace; inline ask_user question cards with option chips and free-text fallback; multi-conversation sidebar with export/delete and per-conversation system-prompt override; model picker across configured providers with per-provider error notes; file tree with context menu (preview/delete); file preview modal with syntax highlighting and line numbers; diff rendering for edit_file calls; image and text-file attachments (drag/drop/paste); stop/cancel mid-turn; settings for temperature/max tokens/max steps.

Constraints:

- Local-first and private (user-confirmed, binding): zero telemetry, no accounts, no cloud dependency beyond the model APIs the user configures; everything stays on-device.
- The backend only ever listens on localhost.

## Brand Commitments

- Keep the terminal aesthetic (user-confirmed, binding): dark zinc palette, monospace type for machine output (tool names, paths, traces), uppercase micro-labels, compact density. It is identity, not a default theme to be replaced.
- Name: YAAH; window title "YAAH - AI Coding Agent".

## Evidence on Hand

- The working app itself: frontend (src/), backend (backend/), tests (backend/tests/), Tauri config (src-tauri/tauri.conf.json).
- No marketing site, screenshots, testimonials, press, or case studies exist. Future work must not fabricate any of these.

## Product Principles

1. Transparency over magic: every agent action is visible, inspectable, and interruptible.
2. Local-first trust: data, keys, and conversations never leave the machine except to model endpoints the user chose.
3. Extensibility without code: users extend the agent by dropping markdown skills into a folder.
4. Density with clarity: terminal-grade compactness, but hierarchy and state (running/done/error) must always read at a glance.
5. Calm under stream: long-running turns must never make the UI jumpy or lossy.
