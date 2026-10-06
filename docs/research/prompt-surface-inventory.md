# YAAH Prompt Surface — Fact Base for the Full Prompt-Surface Review

Baseline: master @ `5dc98b9` (2026-10-06 re-walk, #300; original walk was
main @ `aeba2e7`, 2026-09-30). Source of truth for every fact below.
Commit-pinned by the ticket executor at execution time (the ticket says how).

Purpose: the single fact base for the six prompt-surface-review tickets (#TBD).
Findings are recorded by those tickets; this document is only what the harness
walked and measured at `5dc98b9`.

## 0. Architecture in one paragraph

Prompts are assembled **in Python code, not template files**. There is exactly one
base-prompt builder — `backend/agent/loop.py::_default_system_prompt()`
(loop.py:468–656) — that every chat turn flows through, then `run_agent` /
`_run_agent_claimed` (loop.py:1573 / 1640) appends conditional fragments to it with `\n\n---\n\n` separators.
Tool descriptions live as JSON-schema literals in four modules and are merged by
`backend/agent/tools.py::get_schemas()` (tools.py:1679–1750). Auxiliary model calls
(compaction, title generation) have their own one-off prompts. The only prompt .md
in the repo is the ops-level scheduled-agent prompt.

## 1. The base (default) system prompt

| | |
|---|---|
| **Role** | Base prompt |
| **Where** | `backend/agent/loop.py:468–656` (`_default_system_prompt`) |
| **Size** | ~189 source lines; measured at 5dc98b9 (see findings doc §3): bare base 5,923 B, default local Windows chat 21,014 B, max local combo 22,004 B (win-local-plan-noshot-compaction-sandboxonly) — nothing in the render matrix approaches the folkloric 24 KB, but the older "absolute ceiling ≈19.7 KB" margin is gone |

Trigger: always, unless the conversation has a `system_prompt_override`
(DB column `conversations.system_prompt_override`, backend/db/database.py:63; wins
wholesale — it *replaces*, not composes: loop.py:1758).

Major sections (in output order):

1. Identity line — `"You are an expert AI coding agent working inside a user's project workspace."` (loop.py:544)
2. Runtime-environment line — dynamic, from `_local_env_line()` (loop.py:312–320) or `RemoteSession.env_line()` (remote.py:138–167); includes the shell phrase from `_shell_phrase()` (loop.py:293–309), which composes `windows_bash_note()` (shell.py:67–70) and `CMD_TOOLS_NOTE` (remote.py:111–114)
3. `"You have tools: …"` — a hand-maintained **prose list** of tool names (build block loop.py:492–544, sentence at loop.py:548); Windows adds powershell + 11 computer-use + 4 sandbox tool names (conditional, see §3)
4. Guidelines (loop.py:550–573) — explore-first, gh-CLI-over-scraping, edit-vs-write, test-environment-by-side-effects, sandbox containment, clean-tree failure triage, web research, commit policy, narration, shell cwd/cd lifetime
5. Spoken briefing (`<say>`) contract (loop.py:588–624), ≤400 chars — only rendered when the `voice.say_emissions` toggle is on (#207); narrator personas `SAY_PERSONAS` (loop.py:431–456, #295)
6. Interview-the-user (ask_user) discipline (loop.py:628–641)
7. `computer_section` — appended when local-Windows (see §3)
8. `sandbox_section` — same condition
9. Skills index — only if ≥1 model-invocable skill exists
10. Sub-agent index — always

## 2. Conditional fragments (chat turn assembly)

Assembly flow: `run_agent` → `_run_agent_claimed` (loop.py:1573 / 1640) →
override-or-base selection (loop.py:1758) → fragments appended 1774–1830 →
`messages` with system first (loop.py:1858–1861).

| Fragment | File:lines | Trigger | Approx size |
|---|---|---|---|
| Branch-selector note | branch-note family (loop.py:960–1101: `_selected_branch_note` 960–1043, degraded 1046–1075, stale 1077–1101), appended at loop.py:1816–1817 | every conversation with a stored pin — since #301 every local-git-workspace chat is pinned at creation (inherited: the workspace's branch at creation; explicit: the draft card's pick or a later pick); wording varies by pin origin (`inherited` never claims "the user selected"); carries the amendment rules (immunity/start point) | ~142 src lines / ~1.6 KB |
| Computer-use section | `backend/agent/loop.py:368–417` (`_computer_use_prompt`); embedded `panic_notice()` call (computer.py:1513–1522) at loop.py:417 | `windows AND host is None` (local sessions only) | ~50 src lines / ~2.7 KB |
| Sandbox section | `prompt_section` (backend/agent/sandbox.py:1310–1434), appended in the tools/sandbox block (loop.py:643–646) | local-Windows | ~125 src lines / ~7 KB (largest fragment) |
| Powershell line in tool prose list | loop.py:496 | `windows` | 1 line |
| Computer-use + sandbox names in tool prose list | loop.py:503–514 (computer) / 518–526 (sandbox); `screenshot` omitted when the Settings toggle is off (issue #140; `screenshot_allowed()` tools.py:1653–1664) | local-Windows | ~25 lines |
| Skills index | `backend/agent/skills.py:260–278` (`index_for_prompt`) | ≥1 skill without `disable-model-invocation` in `~/.yaah/skills` (40 bundled skills ship; ~10 manual-only) | ~4–5 KB |
| Sub-agent index + delegation policy | `backend/agent/subagents.py:270–296` (`index_for_prompt`), appended at loop.py:655 | always (2 built-ins guarantee content) | ~27 lines / ~1.7 KB |
| AGENTS.md project notes | `_agents_notes` (loop.py:323–345), appended at loop.py:1783–1785 | `<workspace>/AGENTS.md` exists and non-empty; skipped for `remote:` workspaces; capped at `MAX_AGENTS_NOTES_CHARS = 8_000` (loop.py:290) | wrapper ~6 lines + content |
| Persistent memory block | `backend/agent/memory.py:216–243` (`_WHEN_TO_SAVE`) + `index_for_prompt` (memory.py:303–359), wrapped by `_memory_notes` (loop.py:348–365), appended via loop.py:1789–1791 | `MEMORY.md` index exists and differs from template; capped `MAX_INDEX_CHARS = 12_000` (memory.py:33) | wrapper ~20 lines + index |
| Explicitly invoked skills (`/name`, chips) | `backend/agent/skills.py:292–307` (`bodies_for_prompt`) wrapped via `invoked_skills_wrapper` (loop.py:849–866), called at loop.py:1774–1779 | skill names passed with the turn | header ~13 lines + bodies (each ≤ `MAX_SKILL_BODY_CHARS = 60_000`, skills.py:33) |
| Mid-turn loaded skills (`load_skill`) | `backend/agent/skills.py:310–348` (`load_skill_into_messages`); queued-message variant `_apply_injected_skills` (loop.py:869–893, drained at loop.py:2212 / 2460) | model calls `load_skill`, or queued message carries skill chips; mutates `messages[0]` in place; result carries a `truncated` flag when the parse-time body cap fired | header ~10 lines + body |
| Plan-mode note | `_plan_mode_note` (loop.py:913–924), appended at loop.py:1820–1822 | `current_access_mode() == "plan"` | ~12 lines |
| Sandbox-only (scheduled agent) note | `_sandbox_only_note` (loop.py:1103–1124), appended at loop.py:1826–1828 | scheduled agent with `policy == "sandbox-only"` | ~22 lines |
| Offline-remote note | `remote_runner.py:103–128` (`_system_prompt` wraps the base builder) | remote turn whose owning device is offline | 3 lines |
| Compaction summary injection | `compact_summary_message` (backend/agent/prompt_manifest.py:164–189), used at loop.py:1858–1861 — `"Earlier conversation summary (…):" + summary` | compaction has fired (`prompt_state["summary"]` non-empty) | 1 line + summary (≤ `_SUMMARY_MAX_CHARS = 12_000`, compaction.py:39) |
| Standing instructions (scheduler) | backend/agent/scheduler.py:439–445 | scheduled agent has saved instructions | header + 1 line each |
| Attachment re-inlining | `backend/agent/attachments.py:16–40` (`inline_attachment_text`; re-inline at attachments.py:43) | message has structured attachments (#142); exact format pinned by the byte-identical backend/TS golden fixture (test_attachment_inline.py + src/attachmentFixture.ts) | ~10 lines/attachment |
| MCP tool descriptions | `backend/agent/mcp_client.py:301–323` (`McpManager._discover`) merged by get_schemas (tools.py:1733–1736) | MCP server configured & connected; descriptions come from the server (fallback string); names prefixed `mcp_<server>_<tool>` | dynamic |
| Scheduled-run user prompt | scheduler.py:442 — the agent's own `prompt` field, verbatim | scheduler fire | user-defined |
| `exit_plan` tool schema | `EXIT_PLAN_SCHEMA` (loop.py:1129–1151), appended to tools only in plan mode (loop.py:1885–1886) | plan mode | 1 description (~5 lines) |

## 3. Tool descriptions YAAH defines

All are OpenAI-function-schema literals with `"description"` strings; the registry
is in backend/agent/tools.py (`TOOLS_SCHEMA` + conditional appends, executors map
at tools.py:1456–1476, filtering in `get_schemas()` tools.py:1679–1750).

| Module | Tools (count) | Notes |
|---|---|---|
| `backend/agent/tools.py:170–201` (`TOOLS_SCHEMA`) | 1: bash — `bash` is the longest (~18 lines); the git-editor rule is canonical here (#187, SYN-13) | |
| `backend/agent/tools.py:555` (`TOOLS_SCHEMA +=` GET_HELP_SCHEMA, BRANCH_SELECT_SCHEMA) | 2: get_help (399–420), branch_select (423–451, #277) | get_help/branch_select are agent-offered only (appended at tools.py:555) |
| `backend/agent/tools.py:567–962` (`TOOLS_SCHEMA +=`) | 17: web_search, web_fetch, view_image, ask_user, read_file, write_file, edit_file, create_file, delete_file, move_file, search_files, search_conversation_history, spawn_agent, load_skill, memory_save, memory_read, memory_delete | `spawn_agent` ~16 lines |
| `backend/agent/tools.py:206–233` | 1: powershell (`POWERSHELL_SCHEMA`, Windows-only append, tools.py get_schemas:1694) | |
| `backend/agent/tools.py:237–251` | 1: install_git (Windows + git missing + bundled installer, tools.py get_schemas:1712–1716) | |
| `backend/agent/computer.py:97–366` | 11: read_ui_tree, screenshot, list_windows, focus_window, mouse_move, mouse_click, mouse_drag, mouse_scroll, type_text, press_key, wait (`COMPUTER_TOOLS_SCHEMA`, merged via import tools.py:1482) | 6 input tools embed shared `_HOST_INPUT_NOTE` (computer.py:35–39); `_MONITOR_PARAM`/`_OBSERVE` param descriptions shared |
| `backend/agent/sandbox.py:1444–1534` | 4: sandbox_test/run/status/stop (`SANDBOX_TOOLS_SCHEMA`; stripped for remote sessions, tools.py get_schemas:1721–1731) | the ~23-line mini-playbook is the `sandbox_run` description inside the schema (sandbox.py:~1456–1478); the `sandbox_run` executor def is sandbox.py:1271–1297 |
| `backend/agent/loop.py:1129–1151` | 1: exit_plan (plan mode only, loop.py:1885–1886) | |

**Total: 38 self-defined tool descriptions** (plus parameter-level descriptions,
plus dynamic MCP ones). Canonical matrix: win-local 36 tools / 25,151 B;
win-remote 21 / 12,391 B. Supporting description-like text:

- **Lazy docs tier** — `HELP_DOCS` (tools.py:288–397, 15 tools; updated with the
  computer docs at tools.py:1486) and `COMPUTER_HELP_DOCS` (computer.py:71–95,
  3 tools) served by `get_help` (tools.py:528–552; schema at tools.py:399–420,
  appended tools.py:555; unknown-tool nudge tools.py:547); "the schemas stay
  short; this reaches the model only when it calls get_help".
- **In-band model-directed strings** (not schemas but the model reads them):
  timeout-clamp note (`_clamp_note`, tools.py:1018–1027), screenshot-disallowed
  info (tools.py:1618–1621), plan-block result (`_plan_block_result`,
  loop.py:1166–1177), sandbox-only skip result (`_policy_skip_result`,
  loop.py:1154–1163), sandbox hints (`_missing_command_hint` /
  `_mcp_hint` / `_dialog_stall_hint`, sandbox.py:1196–1215 / 1218–1249 /
  1252–1268), `IMAGES ON PAGE` note (webtools.py:274, inside `web_fetch`
  webtools.py:209–283).

## 4. Other prompts (auxiliary model calls & scaffolding)

| Prompt | File:lines | Purpose / trigger |
|---|---|---|
| Compaction summarizer | `_SUMMARIZER_PROMPT` (backend/agent/compaction.py:260–273), called by `summarize_messages` (compaction.py:276–300); summary clamp `_SUMMARY_MAX_CHARS` (compaction.py:39) | fires when measured prompt_tokens crosses trigger fraction (ADR 0004); JSON-envelope 400-word summary |
| Conversation title | `_generate_conversation_title` (loop.py:145–231) | after first successful turn, only while title is still the auto-slice; 3–6 words |
| Sub-agent system prompt builder | `_sub_agent_system_prompt` (backend/agent/subagents.py:325–410); tool prose via the shared `tool_prose_list` helper (tools.py:268–283, #181) | every `spawn_agent`; composes definition body + env line + tool prose list + guidelines + AGENTS.md notes + skills index + memory block |
| Built-in sub-agent definitions | backend/agent/subagents.py:98–114: `general-purpose`; subagents.py:117–135: `explore` | bodies + descriptions feed both the sub-agent prompt and the parent index |
| Sub-agent budget nudges | subagents.py:476–520 (grace/wrap-up logic; "Start converging now" 507–513; replace-in-place nudge 516–520) | appended as system messages when max_turns nears |
| User-defined sub-agents | `~/.yaah/agents/*.md` body = system prompt (`parse_agent_md` subagents.py:161–206); none in repo | |
| Scheduled implement agent (ops prompt, **not wired into code** — operator-facing) | prompts/scheduled-implement.md (75 lines) | "You are an autonomous implementation agent running on a schedule… one GitHub issue per run"; outside YAAH's runtime prompt surface but ships in-tree |
| Skill loader plumbing | skills.py:1–16 module docstring + `SAMPLE_SKILL_MD` (59–77) — the index line "Skills available (load with the load_skill tool…)" is generated at skills.py:268 | — |
| Vendored third-party prompt | backend/bundled_toolkit/vendor/windows_mcp/tools/scrape.py:54 — "You are a web content extractor…" | inside the vendored windows-mcp server that runs in the sandbox VM; ships in-tree but not part of YAAH's own prompt surface |

## 5. Tests / fixtures covering prompt content

No `__snapshots__`, `*.golden`, or jest snapshots exist. Coverage is substring
assertions (de-facto content guards) plus one true golden fixture:

- `backend/tests/test_attachment_inline.py` + `src/attachmentFixture.ts` — the only real golden fixture (attachment inline format, pinned byte-identically across backend/TS).
- `backend/tests/test_agent.py:559–567` — base prompt (cd-lifetime sentences); `:568+` — override replaces entirely.
- `backend/tests/test_sandbox.py:532–621` — prompt_section carries toolkit/clean-image/windows-mcp/silent-install knowledge (532, 551, 563, 578, 623) + hint-text pins (596, 608); `:161` — bootstrap neuters the interactive git editor.
- `backend/tests/test_computer.py:387–398` — computer-use section mentions "user-activity pause".
- `backend/tests/test_issue_140_screenshot_toggle.py:49–60` (schemas) and `:113–155` (base prompt) — screenshot presence/absence.
- `backend/tests/test_skills.py:56` — index hides manual-only skills; `backend/tests/test_memory.py:109–127` — memory index empty-until-first-save, template-silent, capped; `backend/tests/test_subagents.py:38` — no phantom `git_*` tools in any prompt (#174), `:122` — index lists agents; `backend/tests/test_db.py:117` — `system_prompt_override` persists; `backend/tests/test_access_modes.py:257` — plan note in the loop; `backend/tests/test_issue_181_prose_tool_lists.py` — prose/schema tool-list parity (parent + sub-agent).

## 6. Duplication & drift findings (review targets)

1. **~~Tool prose list advertises phantom `git_*` tools~~ — RESOLVED by #174**
   (2026-09-30): the `git_*` names never had a schema or executor anywhere —
   the host's `/api/remote/exec` dispatches through the same `EXECUTORS` map
   (tools.py:1456–1476), so the remote-forward path was a dead end too;
   `REMOTE_TOOLS` (remote.py:73–83) now lists only executable tools, and
   test_subagents.py:38 (`test_no_phantom_git_tools_in_prompts`) guards parent +
   both built-in sub-agent prompts. The sub-agent prose list is no longer a
   hand-synced copy at all: both prompt prose lists now derive from the real
   tool set (parent: conditionals in `_default_system_prompt` loop.py:492–544;
   sub-agent: `tool_prose_list` tools.py:268–283, #181).
2. **~~Test-environment guidance duplicated~~ — RESOLVED by #186 (SYN-12/17)**:
   sandbox.prompt_section is the SINGLE home for the test-environment rule,
   containment rules, and toolkit persistence; the guideline bullets that
   duplicated ~1 KB of it were removed (comment at loop.py:575–581). The
   git-editor rule remains in the shell-tool descriptions (canonical since
   #187: bash `TOOLS_SCHEMA` tools.py:170–201, echoed in powershell via
   `_GIT_EDITOR_POINTER` tools.py:165–168/219) plus the VM-side bootstrap
   neutering (sandbox.py:504–508) and one prompt_section echo (sandbox.py:1382)
   — deliberate layering, not drift.
3. **gh-CLI preference**: now lives only in the base-prompt guidelines
   (loop.py:553–556); the former tool-description copies are gone. Single
   source, no drift risk.
4. **"Invoked skills" wrapper — duplication resolved by #260**: both injection
   paths now share one helper, `invoked_skills_wrapper` (loop.py:849–866), called
   from the base-injection site (loop.py:1778) and the turn-time site
   (loop.py:891).
5. **`SAY_MAX_CHARS = 400` mirrored** in backend/agent/speak.py:604 and src/speech.ts:118 (documented mirror; drift would break transcript stripping).
6. **windows-mcp playbook tripled**: sandbox prompt_section (sandbox.py:1310–1434), the `SANDBOX_TOOLS_SCHEMA` sandbox_run description (sandbox.py:1444–1534), and `_HOST_INPUT_NOTE` on the 6 input tools (computer.py:35–39).
7. **Per-spawn duplication is growing**: sub-agent prompts now carry
   project-notes + persistent-memory + skills-index per spawn
   (general-purpose with skills: 3,687 B; explore: 1,842 B — §3 of the findings
   doc). Per-spawn, not per-turn, but worth watching as surfaces accrete.
8. **Orphan/legacy**: `_BRIEFING_MAX` (speak.py:607) and `CMD_TOOLS_NOTE`
   sharing (remote.py:111–114) are deliberate.
