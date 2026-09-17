# Plan: Access modes (Ask before changes / Plan mode / Full access)

Status: implemented (2026-09-16). Backend gate + config + sub-agent coverage and frontend mode control + approval card + voice routing; backend suite and tsc/build green.

## Idea

YAAH currently executes every tool unconditionally — "Full access" is the only mode it has. Add a global access mode, switchable live from the header, that gates mutating tools behind the existing ask_user machinery. Mode names and semantics deliberately mirror zcode's permission dropdown so the mental model transfers for users who drive YAAH through it.

## Decisions (user-confirmed)

| # | Question | Decision |
|---|----------|----------|
| Q1 | Mode set | **Three modes.** Ask before changes / Plan mode / Full access. zcode's fourth ("Edit automatically" — file edits free, shell still asks) is dropped as too subtle to earn its keep. |
| Q2 | Scope | **Global, switchable live.** One mode for the whole app, persisted in config.json, changed from a header control. Applies to the next tool call of any conversation, including turns already streaming. No per-conversation modes. |
| Q3 | Plan mode teeth | **Hard block + prompt note + one-click exit.** Mutating/shell tools return an error result ("plan mode is on — present your plan"); the system prompt tells the model it is planning; when the model presents its plan, the blocked-tool card offers "Approve plan & continue" which flips the mode to Full access and resumes the turn. |
| Q4 | Default | **Ask before changes for everyone** — new installs and existing ones. Existing users will see approval prompts after update; that is the point (PRODUCT.md: trustworthy agent loop, defaults are product requirements). |

### Consequences (recorded, not separately decided)

- **Unknown/MCP tools count as mutating.** MCP tools are registered trust with arbitrary behavior; under Ask before changes they prompt, under Plan mode they block. The only classification consistent with Q4's safe-by-default is: unclassifiable = ask. Revisit if MCP tool noise becomes annoying (per-server allowlist is the natural later refinement).
- **Existing users see a behavior change on update.** Accepted in Q4. The first approval card is effectively an announcement of the feature.

## Mode semantics

| Tool class | Tools | Ask before changes | Plan mode | Full access |
|---|---|---|---|---|
| Read-only | read_file, search_files, git_status, git_diff, web_search, web_fetch, view_image, load_skill, ask_user, list/spawn plumbing | free | free | free |
| Mutating files | write_file, edit_file, create_file, delete_file, move_file, git_add, git_commit | **ask first** | blocked | free |
| Shell & remote | bash, powershell, git_push, git_pull, MCP tools, computer-use tools | **ask first** | blocked | free |

- git_add/git_commit are file-mutating but low blast radius; git_push/git_pull are network+repo actions and sit with shell. (git_commit asks, push asks — both under Ask mode; under Plan mode both blocked.)
- spawn_agent is free in all modes: the sub-agent's own tool calls hit the same gate, so delegation cannot launder permissions.

## Design

### Backend (implemented; deviations from the original sketch in brackets)

1. **Classification** — `backend/agent/tools.py`: `tool_risk(name) -> "read" | "mutating" | "shell"` over three set literals (`_READ_TOOLS`, `_MUTATING_TOOLS`, `_SHELL_TOOLS`). Unknown names default to "shell" (safe by default, covers MCP).
2. **Gate** — `backend/agent/loop.py`: `run_gate(name, args, call_id, conversation_id, cancel_ev, mode=None, emit=None)` + `_await_approval` (the ask-mode future wait). The main loop reads the mode once per call, streams `approval_request` itself [the loop yields the request event directly — a generator cannot yield while the gate awaits], then calls `run_gate`; an approved call (`None`) is executed right after, a denial/plan block becomes the tool result. `approval_decision` streams after the gate returns.
3. **Mode source** — `config.py` DEFAULTS `"access_mode": "ask"`; `current_access_mode()` in loop.py validates against `("ask", "plan", "full")` and re-reads `load_config()` per call, so a header switch applies mid-turn without restart. Persisted via the existing `PUT /api/config` (validated, falls back to "ask"); exposed by `GET /api/config`.
4. **Plan-mode prompt note** — `loop.py` injects a `# Access mode: PLAN` section into the system prompt each turn while plan mode is active.
5. **Sub-agents** — `subagents.py` `run_sub_agent(..., gate=)` / `spawn_batch(..., gate=)`: every sub-agent tool call passes through a gate closure the parent loop builds; gate keys are namespaced `"<spawn_call_id>:<tool_call_id>"` so parallel sub-agents cannot collide in the pending-answer map. Approval events ride the existing sub-agent event queue into the stream (frontend reads the forwarded `call_id`, which is already the namespaced key).
6. **Answers** — the existing `POST /api/conversations/{id}/answer` resolves approval futures: `"approve"` executes, `"deny"` returns "user denied…", any free text denies with that text as guidance.

### Frontend (implemented)

7. **Status-strip control** — `AccessModeControl` (cycles ask → plan → full, uppercase mono chip, reverts on save failure) + `PlanExitButton` ("approve plan & run", visible only in plan mode while not streaming) [moved here from the card: plan mode never holds a pending approval — blocked calls fail instantly, so the exit belongs next to the mode control].
8. **Approval card** — `ApprovalCard` (amber, `!` pulse, tool + one-line arg summary, full command block for bash/powershell, Approve / Deny / "Deny with a note…" free text). Screen-scoped via `pendingApproval.convKey` like questions; cleared on error/stop/done.
9. **Trace** — the approval renders as a normal tool chip + result (approved execution or denial error); `approval_request`/`approval_decision` also push into the activity log.
10. **Voice** — PTT routing: approve-words ("approve/allow/yes/ok/go ahead/confirmed") resolve the gate, any other transcript denies with the transcript as guidance; a PTT press no longer kills a turn blocked on an approval (same shield as ask_user questions).
11. **Startup** — `AccessMode` component in App.tsx loads the persisted mode and follows `yaah-access-mode-changed` events (plan-exit dispatches it).

## Tests (backend/tests/test_access_modes.py — 13 tests, all green)

- Classification: read/mutating/shell sets; unknown + MCP names → shell.
- Gate unit: reads pass in every mode; full passes all; plan blocks with the plan-mode error; ask blocks until approve/deny/free-text; cancel unblocks with an error.
- Loop e2e (fake model): ask mode streams approval_request, blocks, denial continues the turn with the error and writes nothing; approve executes for real (file exists); plan mode returns the plan-mode error without executing and injects the `# Access mode: PLAN` prompt section.
- Sub-agent: a `general-purpose` run with a gate stub sees its write_file denied (delegation cannot launder permissions).
- Config API: default "ask"; "plan" round-trips; invalid falls back to "ask".

Plus: the whole backend suite runs with `access_mode: "full"` pinned in conftest (loop tests have no user to answer prompts); `npx tsc --noEmit` and `npm run build` clean.

## Out of scope (this iteration)

- Per-conversation modes, per-tool/per-server allowlists ("always allow bash"), session-scoped "don't ask again".
- Edit-automatically mode (dropped in Q1; can be added later as a fourth entry without schema changes).
- Diff-preview inside the approval card (the card shows tool + args; the existing diff rendering already shows edits in the trace after approval).