# YAAH Prompt Surface — Fact Base for the Full Prompt-Surface Review

Baseline: main @ `aeba2e7` (2026-09-30). Source of truth for every fact below.
Commit-pinned by the ticket executor at execution time (the ticket says how).

Purpose: the single fact base for the six prompt-surface-review tickets (#TBD).
Findings are recorded by those tickets; this document is only what the harness
walked and measured at `aeba2e7`.

## 0. Architecture in one paragraph

Prompts are assembled **in Python code, not template files**. There is exactly one
base-prompt builder — `backend/agent/loop.py::_default_system_prompt()`
(loop.py:309–476) — that every chat turn flows through, then `run_agent` /
`_run_agent_claimed` (loop.py:1258+ / 1325) appends conditional fragments to it with `\n\n---\n\n` separators.
Tool descriptions live as JSON-schema literals in four modules and are merged by
`backend/agent/tools.py::get_schemas()` (tools.py:1487–1551). Auxiliary model calls
(compaction, title generation) have their own one-off prompts. The only prompt .md
in the repo is the ops-level scheduled-agent prompt.

## 1. The base (default) system prompt

| | |
|---|---|
| **Role** | Base prompt |
| **Where** | `backend/agent/loop.py:309–476` (`_default_system_prompt`) |
| **Size** | ~149 source lines; measured at fb85de2 (see findings doc §3): bare base 5,176 B, default local Windows chat 17,785 B, max local combo 18,235 B — nothing in the render matrix approaches the folkloric 24 KB (absolute ceiling ≈19.7 KB) |

Trigger: always, unless the conversation has a `system_prompt_override`
(DB column `conversations.system_prompt_override`, backend/db/database.py:58; wins
wholesale — it *replaces*, not composes: loop.py:1338).

Major sections (in output order):

1. Identity line — `"You are an expert AI coding agent working inside a user's project workspace."` (loop.py:376)
2. Runtime-environment line — dynamic, from `_local_env_line()` (loop.py:205–212) or `RemoteSession.env_line()` (remote.py:138–161); includes the shell phrase from `_shell_phrase()` (loop.py:186–196), which composes `windows_bash_note()` (shell.py:67–70) and `CMD_TOOLS_NOTE` (remote.py:111–114)
3. `"You have tools: …"` — a hand-maintained **prose list** of tool names (loop.py: `You have tools` block); Windows adds powershell + 11 computer-use + 4 sandbox tool names (conditional, see §3)
4. Guidelines (loop.py:382–425) — explore-first, gh-CLI-over-scraping, edit-vs-write, test-environment-by-side-effects, sandbox containment, clean-tree failure triage, web research, commit policy, narration, shell cwd/cd lifetime
5. Spoken briefing (`<say>`) contract (loop.py:427–446), ≤400 chars
6. Interview-the-user (ask_user) discipline (loop.py:448–461)
7. `computer_section` — appended when local-Windows (see §3)
8. `sandbox_section` — same condition
9. Skills index — only if ≥1 model-invocable skill exists
10. Sub-agent index — always

## 2. Conditional fragments (chat turn assembly)

Assembly flow: `run_agent` → `_run_agent_claimed` (loop.py:1258 / 1325) →
`_default_system_prompt` → fragments appended 1341–1390 → `messages` with
system first (loop.py:1420).

| Fragment | File:lines | Trigger | Approx size |
|---|---|---|---|
| Computer-use section | `backend/agent/loop.py:250–299` (`_computer_use_prompt`); embedded `panic_notice()` from `computer.py:1374–1383` | `windows AND host is None` (local sessions only) | ~50 src lines / ~2.7 KB |
| Sandbox section | `prompt_section` (backend/agent/sandbox.py:1217–1338), appended in the tools/sandbox block | local-Windows | ~122 src lines / ~9 KB (largest fragment) |
| Powershell line in tool prose list | loop.py:337–338 | `windows` | 1 line |
| Computer-use + sandbox names in tool prose list | loop.py:339–368; `screenshot` omitted when the Settings toggle is off (issue #140; `screenshot_allowed()` tools.py:1473–1484) | local-Windows | ~25 lines |
| Skills index | `backend/agent/skills.py:242–260` (`index_for_prompt`) | ≥1 skill without `disable-model-invocation` in `~/.yaah/skills` (34 bundled skills ship; 12+ manual-only) | ~4–5 KB |
| Sub-agent index + delegation policy | `backend/agent/subagents.py:267–291` (`index_for_prompt`) | always (2 built-ins guarantee content) | ~26 lines / ~1.7 KB |
| AGENTS.md project notes | `_agents_notes` (loop.py:210–232), appended at loop.py:1333–1335 | `<workspace>/AGENTS.md` exists and non-empty; skipped for `remote:` workspaces; capped at `MAX_AGENTS_NOTES_CHARS = 8_000` (loop.py:183) | wrapper ~6 lines + content |
| Persistent memory block | `backend/agent/memory.py:204–245` (`_WHEN_TO_SAVE` + `index_for_prompt`), appended via loop.py:235–247 / 1339–1341 | `MEMORY.md` index exists and differs from template; capped `MAX_INDEX_CHARS = 12_000` | wrapper ~20 lines + index |
| Explicitly invoked skills (`/name`, chips) | `backend/agent/skills.py:270–284` (`bodies_for_prompt`) wrapped via `invoked_skills_wrapper` (loop.py:716–729), called at loop.py:1409 | skill names passed with the turn | header ~13 lines + bodies (each ≤ `MAX_SKILL_BODY_CHARS = 60_000`, skills.py:33) |
| Mid-turn loaded skills (`load_skill`) | `backend/agent/skills.py:289–336` (`load_skill_into_messages`); queued-message variant loop.py:669–691 (`_apply_injected_skills`) | model calls `load_skill`, or queued message carries skill chips; mutates `messages[0]` in place; result carries a `truncated` flag when the parse-time body cap fired | header ~10 lines + body |
| AGENTS.md project notes | `_agents_notes` (loop.py:210–232), appended at loop.py:1371–1373 | `<workspace>/AGENTS.md` exists and non-empty; skipped for `remote:` workspaces; capped at `MAX_AGENTS_NOTES_CHARS = 8_000` (loop.py:183) | wrapper ~6 lines + content |
| Persistent memory block | `backend/agent/memory.py:204–245` (`_WHEN_TO_SAVE` + `index_for_prompt`), appended via loop.py:235 / 1377–1381 | `MEMORY.md` index exists and differs from template; capped `MAX_INDEX_CHARS = 12_000` | wrapper ~20 lines + index |
| Explicitly invoked skills (`/name`, chips) | `backend/agent/skills.py:263–277` (`bodies_for_prompt`) wrapped at loop.py:1341–1367 | skill names passed with the turn | header ~13 lines + bodies (each ≤ `MAX_SKILL_BODY_CHARS = 60_000`, skills.py:33) |
| Mid-turn loaded skills (`load_skill`) | `backend/agent/skills.py:280–315` (`load_skill_into_messages`); queued-message variant loop.py:669–691 (`_apply_injected_skills`) | model calls `load_skill`, or queued message carries skill chips; mutates `messages[0]` in place | header ~10 lines + body |
| Plan-mode note | `_plan_mode_note` (loop.py:711–722), appended at loop.py:1346–1347 | `current_access_mode() == "plan"` | ~11 lines |
| Sandbox-only (scheduled agent) note | `_sandbox_only_note` (loop.py:725–738), appended at loop.py:1350–1353 | scheduled agent with `policy == "sandbox-only"` | ~13 lines |
| Offline-remote note | `remote_runner.py:103–128` (`_system_prompt` wraps the base builder) | remote turn whose owning device is offline | 3 lines |
| Compaction summary injection | loop.py:1383–1387 — `"Earlier conversation summary (for context only):\n" + summary` | compaction has fired (`prompt_state["summary"]` non-empty) | 1 line + summary (≤ `_SUMMARY_MAX_CHARS`) |
| Standing instructions (scheduler) | backend/agent/scheduler.py:293–301 | scheduled agent has saved instructions | header + 1 line each |
| Attachment re-inlining | `backend/agent/attachments.py:15–27` (`inline_attachment_text`) | message has structured attachments (#142); exact format pinned by the byte-identical backend/TS golden fixture (test_attachment_inline.py + src/attachmentFixture.ts) | ~10 lines/attachment |
| MCP tool descriptions | `backend/agent/mcp_client.py:147–169` (`_discover`, mcp_client.py:147+) merged by tools.py:1541–1549 | MCP server configured & connected; descriptions come from the server (fallback string); names prefixed `mcp_<server>_<tool>` | dynamic |
| Scheduled-run user prompt | scheduler.py:295–301 — the agent's own `prompt` field, verbatim | scheduler fire | user-defined |
| `exit_plan` tool schema | `EXIT_PLAN_SCHEMA` (loop.py:743–765), appended to tools only in plan mode (loop.py:1407–1409) | plan mode | 1 description (~5 lines) |

## 3. Tool descriptions YAAH defines

All are OpenAI-function-schema literals with `"description"` strings; the registry
is in backend/agent/tools.py (`TOOLS_SCHEMA` + conditional appends, executors map
at tools.py:1293–1334, filtering in `get_schemas()` tools.py:1487–1551).

| Module | Tools (count) | Notes |
|---|---|---|
| `backend/agent/tools.py` | 19: bash (146–183), get_help (353–374), web_search, web_fetch, view_image, ask_user, read_file, write_file, edit_file, create_file, delete_file, move_file, search_files, search_conversation_history, spawn_agent, load_skill, memory_save, memory_read, memory_delete (406–805) | `bash` is the longest (~18 lines); `spawn_agent` ~16 lines |
| `backend/agent/tools.py:188–221` | 1: powershell (`POWERSHELL_SCHEMA`, Windows-only append, tools.py get_schemas:1495) | |
| `backend/agent/tools.py:225–238` | 1: install_git (Windows + git missing + bundled installer, tools.py get_schemas:1518–1524) | |
| `backend/agent/computer.py:92–361` | 11: read_ui_tree, screenshot, list_windows, focus_window, mouse_move, mouse_click, mouse_drag, mouse_scroll, type_text, press_key, wait (`COMPUTER_TOOLS_SCHEMA`, merged tools.py:1318–321) | 7 input tools embed shared `_HOST_INPUT_NOTE` (computer.py:31–35); `_MONITOR_PARAM`/`_OBSERVE` param descriptions shared |
| `backend/agent/sandbox.py:1348–1448` | 4: sandbox_test/run/status/stop (`SANDBOX_TOOLS_SCHEMA`, merged tools.py:1330–1333; stripped for remote sessions, tools.py get_schemas:1526–1532) | `sandbox_run` description is a mini-playbook (~23 lines) |
| `backend/agent/loop.py:743–765` | 1: exit_plan (plan mode only) | |

**Total: 37 self-defined tool descriptions** (plus parameter-level descriptions,
plus dynamic MCP ones). Supporting description-like text:

- **Lazy docs tier** — `HELP_DOCS` (tools.py:242–351, 14 tools) and
  `COMPUTER_HELP_DOCS` (computer.py:67–90, 3 tools) served by `get_help`
  (tools.py:377–401); "the schemas stay short; this reaches the model only when it calls get_help".
- **In-band model-directed strings** (not schemas but the model reads them):
  timeout-clamp note (`_clamp_note`, tools.py:860–871), get_help error nudge
  (tools.py:1401–1411), screenshot-disallowed info (tools.py:1449–1459),
  plan-block result (`_plan_block_result`, loop.py:862–873), sandbox-only skip
  result (`_policy_skip_result`, loop.py:850–861), sandbox hints
  (`_missing_command_hint` / `_mcp_hint` / `_dialog_stall_hint`,
  sandbox.py:1103–1175),
  `IMAGES ON PAGE` note (webtools.py:274).

## 4. Other prompts (auxiliary model calls & scaffolding)

| Prompt | File:lines | Purpose / trigger |
|---|---|---|
| Compaction summarizer | `_SUMMARIZER_PROMPT` (backend/agent/compaction.py:260–272), called by `summarize_messages` (275–299) | fires when measured prompt_tokens crosses trigger fraction (ADR 0004); JSON-envelope 400-word summary |
| Conversation title | `_generate_conversation_title` (loop.py:79–124) | after first successful turn, only while title is still the auto-slice; 3–6 words |
| Sub-agent system prompt builder | `_sub_agent_system_prompt` (backend/agent/subagents.py:314–363) | every `spawn_agent`; composes definition body + env line + its own tool prose list + guidelines + AGENTS.md notes + skills index |
| Built-in sub-agent definitions | backend/agent/subagents.py:90–132: `general-purpose` (93–107), `explore` (115–131) | bodies + descriptions feed both the sub-agent prompt and the parent index |
| Sub-agent budget nudges | subagents.py:423–454 (grace turn at 423–432) | appended as system messages when max_turns nears ("Start converging now…") |
| User-defined sub-agents | `~/.yaah/agents/*.md` body = system prompt (`parse_agent_md` subagents.py:158–203); none in repo | |
| Scheduled implement agent (ops prompt, **not wired into code** — operator-facing) | prompts/scheduled-implement.md (75 lines) | "You are an autonomous implementation agent running on a schedule… one GitHub issue per run"; outside YAAH's runtime prompt surface but ships in-tree |
| Skill loader plumbing | skills.py:1–59 module docstring + `SAMPLE_SKILL_MD` (51–59) — the index line "Skills available (load with the load_skill tool…)" is generated at skills.py:250–260 | — |
| Vendored third-party prompt | backend/bundled_toolkit/vendor/windows_mcp/tools/scrape.py:53–54 — "You are a web content extractor…" | inside the vendored windows-mcp server that runs in the sandbox VM; ships in-tree but not part of YAAH's own prompt surface |

## 5. Tests / fixtures covering prompt content

No `__snapshots__`, `*.golden`, or jest snapshots exist. Coverage is substring
assertions (de-facto content guards) plus one true golden fixture:

- `backend/tests/test_attachment_inline.py` + `src/attachmentFixture.ts` — the only real golden fixture (attachment inline format, pinned byte-identically across backend/TS).
- `backend/tests/test_agent.py:515–537` — base prompt (cd-lifetime sentences) + override replaces entirely.
- `backend/tests/test_sandbox.py:516–621` — 7 tests asserting prompt_section/sandbox descriptions carry toolkit, clean-image, windows-mcp, silent-install, firewall, dispose content.
- `backend/tests/test_computer.py:383–390` — computer-use section mentions "user-activity pause".
- `backend/tests/test_issue_140_screenshot_toggle.py:113–137` — screenshot presence/absence in base prompt.
- `backend/tests/test_skills.py:72–85`, `test_memory.py:109–131`, `test_subagents.py:83+`, `test_db.py:117–128`, `test_remote_runner.py:146–150`, `test_access_modes.py:257` (plan note), `test_sandbox.py:173` (git-editor warning in schemas).

## 6. Duplication & drift findings (review targets)

1. **Tool prose list is hand-synced with `get_schemas()`** in two places: parent
  prompt (loop.py:332–368) and sub-agent prompt (subagents.py:329–346). The
  sub-agent copy advertises `"git tools (git_status, git_diff, git_add, git_commit, git_push, git_pull)"`, but **no `git_*` executor exists in `EXECUTORS`**
  (tools.py:1293–1312) — they are remote-forward-only (`REMOTE_TOOLS`,
  remote.py:73–89). Locally a sub-agent calling `git_commit` gets "Unknown tool".
  The parent prompt does not list them → parent/sub-agent inconsistency to verify.
2. **Test-environment guidance appears twice nearly verbatim**: base-prompt bullet
  (loop.py:389–406) and sandbox prompt_section (sandbox.py:1219–1238); the "git
  must never open its editor" rule appears in both `bash` and `powershell`
  descriptions, and again in sandbox prompt_section (sandbox.py:1283–1289).
3. **gh-CLI preference** duplicated: bash description (tools.py:165–168),
  powershell description (tools.py:204–207), base prompt guideline (loop.py:384–385).
4. **"Invoked skills" wrapper — duplication resolved by #260**: both injection
  paths now share one helper, `invoked_skills_wrapper` (loop.py:721–729), called
  from the base-injection site and the turn-time site (loop.py:757 / 1414).
5. **`SAY_MAX_CHARS = 400` mirrored** in backend/agent/speak.py:498 and src/speech.ts:118 (documented mirror; drift would break transcript stripping).
6. **windows-mcp playbook tripled**: sandbox prompt_section (sandbox.py:~1283–1320), `sandbox_run` description (sandbox.py:1384–1413), and `_HOST_INPUT_NOTE` on every input tool (computer.py:31–35).
7. **Orphan/legacy**: `_BRIEFING_MAX`, `CMD_TOOLS_NOTE` sharing is deliberate. The `git_*` names were removed with #174 (2026-09-30): they never had a schema or executor anywhere — the host's `/api/remote/exec` dispatches through the same `EXECUTORS` map, so the remote-forward path was a dead end too; `REMOTE_TOOLS` now lists only executable tools.
