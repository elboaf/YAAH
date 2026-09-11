# Plan: Workspace-organized conversation history

Status: decided (grilled 2026-09-11). Every open question below was answered by the user; consequences are recorded where an answer overrode the recommendation.

## Idea

Organize the sidebar conversation history by workspace. Existing workspaces become a dropdown selection. One archive, grouped; the dropdown is the primary switcher.

## Decisions (user-confirmed)

| # | Question | Decision |
|---|----------|----------|
| Q1 | Filter vs grouping | **Grouping.** The list always shows all conversations under collapsible workspace headers. No filter mode. |
| Q2 | Dropdown semantics | **Dropdown = conversation switcher.** Picking a workspace opens its most recent conversation (or a fresh chat if it has none). |
| Q3 | Picker form | **Native `<select>`** of known workspaces + pinned "Add workspace…" item (opens folder browser). Typed paths die. |
| Q4 | Source of truth | **Registry table** (`workspaces`), not `DISTINCT` over conversations. |
| Q5 | Re-filing conversations | **No.** A conversation's workspace is immutable after creation. Row menu stays export / sys / delete. |
| Q6 | Unfiled conversations | Live in a **"Default"** pseudo-workspace: no root directory, not a filesystem path. |
| Q7 | Scope | Grouping + dropdown + **row-action menu rework** (labeled hover menu replaces `md↓ / sys / ✕` glyphs). No search, no keyboard nav this iteration. |
| Q8 | Assembly | Chevron collapses; header body click = open most recent (same as dropdown). Collapse state per workspace in localStorage, start expanded. Rows: `updated_at` desc, title + relative timestamp. Header: basename, full path tooltip, "missing" marker for dead paths. |
| Q9 | Default → real adoption | **No.** Default is permanent. A thread started before picking a workspace stays rootless forever; fix is delete-and-re-ask. |
| Q10 | New chat's workspace | **Inherits the open conversation's workspace** (Default on fresh launch). Filing happens on first send. |
| Q11 | Switching mid-stream | **Free, no confirm.** Requires per-conversation stream buffers (see Store refactor). |
| Q12 | Removing a workspace | **Remove action on the group header**; its conversations relocate to Default. One dialog warns "N conversations will move to Default." Default cannot be removed. |
| Q13 | Row actions | **Active row only, hover-only** reveal (matches today's active-row pattern, now a labeled `⋯` menu instead of bare glyphs). |
| Q14 | Amber "can't see files" note | **Removed.** |
| Q15 | File tools in Default | **Unchanged cwd behavior.** The model tries the request against the process cwd; if it can't find a file it says so or broadens the search. No special-casing, no refusal. |

### Accepted hazards (recorded, not bugs)

- **Q15:** in a packaged Tauri build, a Default conversation's file tools resolve against the sidecar's spawn folder (an install-ish directory), not a user project. Reads return not-found; writes could land there. User explicitly accepts this ("the model decides what to do"). Shell and web tools were never sandboxed.
- **Q9:** the most common first-run mistake (chatting before adding a workspace) produces permanently rootless threads. Accepted; Default is honest about being rootless.
- **Q11:** free mid-stream switching means a background turn keeps writing into its conversation's buffer with no supervision. Accepted; history is still persisted per conversation.

## Core invariant

**The open conversation's workspace IS the active workspace — in both directions, always.**

- Dropdown → opens that workspace's most recent conversation (active workspace follows).
- Row click → opens that conversation and sets the active workspace to its workspace (including Default), even from another group in the same view.
- Nothing else ever changes either side. This replaces today's one-way hijack at `components.tsx:1004` and makes the file tree, preview, and traces always match the open thread.

## Data model

### New table `workspaces`

```sql
CREATE TABLE IF NOT EXISTS workspaces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT UNIQUE,            -- NULL = Default (rootless)
  label TEXT NOT NULL,         -- basename for display; 'Default' for the pseudo-workspace
  last_opened_at TEXT,         -- touched on switch and on turn start
  created_at TEXT NOT NULL
);
```

- Default is a registry row with `path = NULL`, label `Default`. Guard the insert (SQLite UNIQUE permits multiple NULLs).
- `conversations.workspace` keeps storing strings. **Default is represented as `NULL`/`''` in existing rows — no conversation rewrite.** Grouping uses `COALESCE(NULLIF(workspace, ''), 'Default')`. One normalization: `'.'` (the old `DEFAULT_WORKSPACE`) → `NULL`.

### Migration (one-time, idempotent, on backend startup)

1. Create `workspaces` table.
2. `UPDATE conversations SET workspace = NULL WHERE workspace IN ('', '.')`.
3. Seed registry: `INSERT OR IGNORE` Default row; then one row per `SELECT DISTINCT workspace FROM conversations WHERE workspace IS NOT NULL` (label = basename, `last_opened_at` = that workspace's max `conversations.updated_at`).
4. Seed from `config.json` `last_workspace` if not present.

### Path normalization (dedupe)

Add-time: backend resolves the path (`Path.resolve()`), stores the resolved absolute string. Dedupe key = case-folded resolved path (Windows case-insensitivity). Trailing slashes and `..` disappear via resolve.

## Backend changes (`backend/`)

- `db/database.py`: table + migration + registry CRUD + `list_workspaces()`.
- `main.py` endpoints:
  - `GET /api/workspaces` → `[{id, path, label, last_opened_at, exists, conversation_count}]` (`exists` = `os.path.isdir` for real paths, `true` for Default).
  - `POST /api/workspaces` `{path}` → resolve, dedupe, upsert, touch `last_opened_at`.
  - `DELETE /api/workspaces/{id}` → relocate its conversations to Default (`UPDATE conversations SET workspace = NULL`), delete row. 400 on Default.
- `create_conversation` already accepts workspace; sending `''`/`None` files under Default — unchanged.
- `set_last_workspace` on turn start (`main.py:171`) stays; also touch the registry row there.
- File tools: **no change** (Q15). `resolve_path('')` → cwd, which is today's behavior for rootless conversations.

## Frontend changes (`src/`)

### `store.ts` — per-conversation buffers (the meaty refactor)

Today `messages` is one global array; a stream in flight appends to whatever is loaded, so switching mid-turn corrupts the view. Required shape:

- `messagesByConv: Record<string, ChatMessage[]>` keyed by conversation id, plus `'draft'` for the unsaved new chat.
- The send path captures its target key at send time (`conversationId ?? 'draft'`); all streaming appends, tool ticker events, status, error, and pendingQuestion for that turn write to the captured key — never to "whatever is on screen".
- The view renders `messagesByConv[currentId ?? 'draft']`.
- On first send, the draft buffer moves to the created conversation's key (id known after `createConversation`).
- `loadHistory` writes into the target conversation's key, not blindly to `messages`.
- Workspace state: `'Default'` sentinel or absolute path; `setWorkspace` keeps localStorage + `config.json` persistence (existing keys stay).

### `components.tsx` — sidebar restructure

- **Workspace `<select>`** replaces the text input + `browse...` + amber note (lines ~1188–1209). Options: registry workspaces ordered by activity (group order), then pinned `Add workspace…` (opens the existing Tauri `pick_workspace` flow; in browser dev, hidden or no-op with a hint). Selection persists via the existing `last_workspace` path.
- **Grouped list** replaces flat `ConversationList`:
  - Group header: chevron (collapse/expand, persisted per workspace in localStorage) + basename + missing marker (from `exists`) + full path as tooltip. Default's header is just "Default", no path. Header body click = open most recent conversation in that workspace.
  - Rows: title + relative timestamp, `updated_at` desc. Click = open conversation + set active workspace to its workspace (the invariant).
  - Row actions (active row, hover-only): `⋯` menu with labeled items — Export as Markdown / System prompt override / Delete — reusing the existing `NoticeDialog` / `PromptDialog` / `ConfirmDialog`. The `md↓ / sys / ✕` glyph strip dies.
  - Remove action on real group headers → `ConfirmDialog` "N conversations will move to Default."
- **Empty state** (`components.tsx:1639`) and first-run copy: teach "pick a workspace (or add one) → pick a model → ask". No workspace + no conversations = Default group with the empty-state text.
- Mid-stream switching: no guard anywhere (Q11) — delete rather than add.

## Edge cases

| Case | Behavior |
|------|----------|
| Switch workspace mid-stream | Free; stream continues into its own buffer (Q11c). |
| Open conversation from another group | Active workspace follows the conversation (invariant). |
| Delete the open conversation | Existing behavior: fall back to a fresh draft in the same workspace. |
| Remove a workspace whose conversation is open | Conversations relocate to Default; view follows to Default. |
| Dead path selected | Selectable; file tree shows its error state; file tools fail naturally; marker shows in header + dropdown option. |
| Duplicate path (case/slash variants) | Deduped at add-time via resolved, case-folded key. |
| Fresh install, empty registry | Dropdown shows Default + Add workspace…; first-run flow teaches the order. |
| Legacy rows with `workspace = '.'` | Normalized to NULL (Default) in migration. |

## Out of scope (this iteration)

Search/filter over conversations; keyboard shortcuts (new chat / switch / settings); per-conversation stream multiplexing on the backend; workspace rename/label editing; adopting Default conversations into a workspace (rejected in Q9); any change to file-tool sandboxing (rejected in Q15).

## Verification

- `pytest` (backend): migration idempotence; registry CRUD; DELETE relocation; `GET /api/workspaces` shape (`exists`, counts); Default filing via empty workspace.
- `npm run build` + `npm run dev` manual pass: grouped list renders from real DB; dropdown switch opens most-recent; mid-stream switch leaves both threads intact; remove-workspace relocation; dead-path marker; fresh-DB first run.
- Packaged smoke (sidecar): Default conversation file-tool behavior matches cwd expectation (Q15, accepted hazard).
