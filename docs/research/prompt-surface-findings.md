# Prompt-surface review — synthesis of findings (areas 2–5)

Spec: elboaf/YAAH#160. Synthesis ticket: elboaf/YAAH#166.

**Baseline: master @ `fb85de2140add03f687bcf3fc5a68aa57f0cd92a`** (2026-09-30,
"Fix CI: two test-isolation landmines the harness tripped"), worktree
`C:\Users\Administrator\YAAH-prompt-review`. All `file:line` references below are
to that commit. Fact base: `docs/research/prompt-surface-inventory.md` (pinned to
`aeba2e7`; drift re-checks in the area reports found none in reviewed files
except where noted). Evidence base: the 140 committed manifests in
`backend/prompt_manifests/` plus targeted fresh `--render` runs made by the area
reviews (never `--all`; the worktree was not modified).

This document merges the four area reviews into one consolidated, deduplicated
list. Source reports (uncommitted working papers, deliberately not committed):

| Area | Report | Findings |
|---|---|---|
| 2/6 base prompt + conditional fragments | `.scratch/review-2-base-fragments.md` (ticket #162) | F2-1 … F2-16 |
| 3/6 tool descriptions, get_help tier, in-band strings | `.scratch/review-3-tools-docs.md` (ticket #163) | F3-1 … F3-20, F3-21 |
| 4/6 sub-agent path + auxiliary prompts | `.scratch/review-4-subagents-aux.md` (ticket #164) | F4-1 … F4-11 |
| 5/6 injected content + injection mechanisms | `.scratch/review-5-injection.md` (ticket #165) | F5-1 … F5-12 |

Raw input: 59 findings + 1 verification record (F3-21). After cross-area merge:
**49 consolidated findings** (SYN-01 … SYN-49). Severity scale is the spec's:
Critical = prompt asserts something code contradicts on a reachable path; High =
fragments contradict each other or advertise wrong tools; Medium = duplication
with real drift risk or a wrong cap/limit; Low = duplication without drift risk,
wording inconsistency.

**Deduped counts by severity: Critical 3 · High 10 · Medium 21 · Low 15.**

---

## 1. Consolidated findings

Severity shown is the consolidated call. Where two areas disagreed, the
reconciliation is recorded in §4 (contradictions) and the row notes both source
severities.

### Critical (3)

| ID | Sources | Finding | Evidence | Suggested fix | Conf. |
|---|---|---|---|---|---|
| SYN-01 | F2-1 | Computer-use and sandbox sections glued with no separator in every local Windows prompt | `backend/agent/loop.py:441-442` concatenates raw while every other join uses `\n\n---\n\n`; sandbox header lands mid-bullet after the panic-hotkey line (byte-verified in `win-local-compaction`, identical in all 128 win-local manifests) | Prepend `"\n\n---\n\n"` to `sandbox_section` (or trailing-newline `_computer_use_prompt`); add a manifest assertion that no section-opening `#` appears mid-line | high |
| SYN-02 | F4-1 (Critical), F3-2 (High) | Sub-agent prompt advertises six `git_*` tools that cannot execute anywhere — no executors, no schemas, and the remote-forward escape hatch is a dead end (host `/api/remote/exec` dispatches the same executor map and rejects the names) | `subagents.py:336-338` prose vs empty `EXECUTORS` (`tools.py:1293-1312`); manifest `kind-subagents-win-skills.json` sections[1]; host dispatch `backend/main.py:2516-2538`; parent list (loop.py:327-369) correctly omits them. Local call → deterministic `Unknown tool` mid-task, every run, all four manifest variants | Delete the git-tools prose entry (gh CLI via bash is the supported path), or add real schemas+executors so `EXECUTORS`/`REMOTE_TOOLS`/both prose lists/`get_help` agree; fix inventory §6.7's host-side-schemas claim | high |
| SYN-03 | F3-1 | `view_image` help doc says the image is visible "on the NEXT turn"; the loop attaches it before the next model call of the same turn | `HELP_DOCS["view_image"]` (`tools.py:280-286`) vs executor path `webtools.py:311-361` → `loop.py:1877-1890` (`_image_part` appended to messages same turn) | Reword: "a vision-capable model sees it on the next model call (usually immediately after this tool result)" | high |

### High (10)

| ID | Sources | Finding | Evidence | Suggested fix | Conf. |
|---|---|---|---|---|---|
| SYN-04 | F2-2 | `RemoteSession.windows` is `True` for every remote host: non-Windows hosts would be told "Windows 11" and handed the PowerShell tool | `remote.py:83-84` (misnamed flag), producer sends `windows: true` unconditionally; `loop.py:385-388` appends powershell for all remote sessions; `remote.py:178-183` forwards it; `win-remote-compaction` tool list + env line | Make the handshake carry a real `os`/`platform` capability (`host_info()` already gathers it) and gate the powershell tool/env line on it | high on mechanism/bytes; medium that a non-Windows host is connectable today |
| SYN-05 | F2-3 | Sandbox-only note claims every approval-requiring tool is unavailable, but the gate skips only a fixed list — the four `sandbox_*` tools still run (and install persistent host-side tooling) in an unattended run | Note `loop.py:703-713` vs gate `loop.py:1797-1811`; `win-local-plan-compaction-sandboxonly` carries both the note and all four sandbox schemas; `sandbox_run` description advertises host persistence | Add sandbox tools to the skip set, or narrow the note to "host file edits and shell commands are unavailable; sandbox VM tools still work" | high |
| SYN-06 | F2-4 | Offline-remote turn path drops the plan-mode note but keeps `exit_plan`: `win-remote-offline-plan.json` and `-normal.json` are byte-identical (`0193f6b7…`, 16,293 B) while `tool_schemas` include `exit_plan` | `remote_runner.py:96-107` appends only the offline sentence; local path `loop.py:1338-1341` appends plan/sandbox-only fragments; manifest byte-identity (by construction, `prompt_manifest.py:711-717`) | Factor the plan/sandbox-only fragment block into a helper both paths call; add harness assertion that combos differing only in `plan` differ exactly in the plan-note section | high on bytes; medium on production reachability of plan+offline-remote |
| SYN-07 | F2-5 | POSIX prompts carry Windows-sandbox instructions for tools the session doesn't have (`sandbox_test`/`sandbox_run`/`windows-mcp` named in guidelines, absent from the 19-tool schema set; Windows Sandbox doesn't exist there at all) | Guidelines are a static string `loop.py:376-414`; `posix-local` render 6,699 B / 19 tools, guidelines ≈2.6 KB still Windows-flavored | Split guidelines into platform-neutral core + Windows-only block appended when `windows` (the builder already computes it) | high |
| SYN-08 | F3-3 | bash help doc: "On timeout the whole process tree is killed — partial output is still returned" is false: output is discarded, only `[timed out after Ns]` is returned | Doc `tools.py:260-267` vs executor `tools.py:898-899`, `run_bash` `tools.py:910-936`; kill-tree itself is real (`tools.py:831-849`) | Return accumulated output before the marker, or correct the doc ("output is discarded; run in chunks") | high |
| SYN-09 | F4-2 (High), F4-10 (Med), F3-4 (Med) | Hand-maintained prose tool lists drift from the real schema sets in both directions: sub-agent list omits `get_help` + 3 memory tools it receives, advertises write tools to `explore` that `_EXPLORE_TOOLS` strips, advertises phantom git tools (SYN-02); parent list never mentions `search_conversation_history` anywhere; delegation-policy text tripled (index paragraph, `spawn_agent` schema, `HELP_DOCS`) | Sub-agent: `subagents.py:330-339` vs `_resolve_tools` `subagents.py:297-311` + allowlist `:84-87`; parent: `loop.py:327-369`; renders in `kind-subagents-win-skills.json` and `win-local-noshot.json` | Derive the prose line from the same computation as `_resolve_tools` (one `tool_prose_list(names)` helper shared by loop.py and subagents.py); one canonical delegation-policy constant | high |
| SYN-10 | F5-2 (High; Critical reading defensible, weighed) | Sub-agents receive `memory_save/read/delete` whose descriptions claim "the index of saved memories is in your system prompt every turn" — `_sub_agent_system_prompt` never injects it; project memories are invisible to sub-agents | `tools.py:728-729` vs `subagents.py:314-363` (no memory section); `kind-subagents-win-skills.json` has skills index, no memory section; executors would work (`memory.py:52-69`) | Inject `memory.index_for_prompt(workspace)` in `_sub_agent_system_prompt`, or scope memory tools out of sub-agent schemas and fix the description | high |
| SYN-11 | F5-6 | 100 KB inline-attachment cap exists only in the UI composer; the backend re-inliner accepts unbounded `content` records from `load_history` rows and the unbounded `attachments` request field — "files at or under this ride inline" is unenforced on a reachable path | `INLINE_LIMIT_BYTES` `attachments.py:12` enforced only at `src/components.tsx:8953`; `inline_attachment_text` `attachments.py:15-29`, `reinline_attachments` `:32-44`; `backend/main.py:918`, `:1127` | Enforce at re-inline time (degrade to staged-path form or truncate with marker); reject oversize `content` at `/api/agent` with 413 | high |
| SYN-20 | F5-1 (High), F2-13 (Med) | HARNESS: every committed `-compaction` manifest is missing the compaction-summary fragment it exists to show — the bootstrap warm-up consumes the fixture watermark, `compact_conversation` rejects the stale watermark, the summary silently never persists; the summary-injection path has zero rendered-byte coverage | 0 of 33 `*-compaction.json` manifests contain the section; fixture `prompt_manifest.py:660-665` vs rejection `database.py:896-899` and warm-up `:84-113`; `SECTION_OPENINGS` anticipates it (`prompt_manifest.py:148`) | Fix the fixture watermark (capture the real last message id); add a harness self-check that `-compaction` renders contain the section | high |
| SYN-49 | F5-3 | HARNESS: sub-agent manifests render with an empty workspace, so the AGENTS-notes block the production builder does inject is invisible — byte counts and contradiction reviews driven from these manifests model the wrong prompt | `prompt_manifest.py:745` renders `_sub_agent_system_prompt(defn, "")`; `_agents_notes("")` → `""` (`loop.py:216-222`); `kind-subagents-win-skills.json` shows `has project-notes: False` | Render sub-agent manifests against the harness fixture workspace (`LOCAL_WS` already carries a fixture `AGENTS.md`) | high |

### Medium (21)

| ID | Sources | Finding | Evidence | Suggested fix | Conf. |
|---|---|---|---|---|---|
| SYN-12 | F2-6 | Test-environment-choice guidance duplicated near-verbatim (base-prompt bullet vs sandbox-section opener), drift already visible | `loop.py:384-390` (632 B) vs `sandbox.py:1158-1171` (611 B, ~93% same; 215 B byte-identical core); both in every win-local render (~1.2 KB) | Keep the decision rule only in the base prompt; sandbox section keeps mechanics only | high |
| SYN-13 | F2-7 (Med), F3-11 (Med) | "git must never open its editor" rule ×3 model-facing copies with 3 wordings (+1 code comment +1 GIT_EDITOR-adjacent bullet); facts differ between copies (sandbox copy has the GIT_EDITOR-preset fact; shell copies have `tag -a`) | bash desc `tools.py:160-166`, powershell desc `tools.py:200-208`, sandbox section `sandbox.py:1222-1228`; measured ~880 B rendered (F2-7) / ~954 B source (F3-11) — same cluster, counted once here (see §4) | One canonical statement (bash schema or a shared constant); sandbox keeps only the VM-specific GIT_EDITOR-preset delta | high |
| SYN-14 | F2-8 (Med), F3-12 (Low) | gh-CLI-over-scraping preference ×3 (bash desc, powershell desc, guidelines); only the guidelines copy carries the substantive facts (bundled gh, auth, don't scrape) | `tools.py:164-169`, `tools.py:203-208` (normalized-identical stubs, ~220 B each), `loop.py:377-381` | Keep the full statement in the guidelines; drop or pointer-ize the schema copies | high |
| SYN-15 | F3-9 (Med), F3-10 (Med) | windows-mcp GUI playbook stated 3-4× (~2.9 KB per local prompt: section bullet 1,173 B + `sandbox_run` desc 334 B + `_HOST_INPUT_NOTE` 237 B × 6 input tools) and the copies already drift on transport/troubleshooting facts (`~1-3s` cost placement, `vm-capture.ps1`, two different MCP-discovery stories, 3-step connect recipe only in the section) | `sandbox.py:1229-1263`, `sandbox.py:1358-1364`, `computer.py:31-35` used at 6 sites, `sandbox.py:1074-1091` vs `:1236-1241` vs `:1246-1258`; refined: 6 tools carry the note, not 7 | Keep the full playbook once (sandbox section); `sandbox_run` desc → pointer; shrink `_HOST_INPUT_NOTE` to one clause; one discovery story | high |
| SYN-16 | F3-14 | Sandbox↔host guidance cluster duplicated between section and schemas across five sub-clusters, ≈2.3 KB per local prompt (clean-image/state.json, toolkit-PATH recipe, dispose, silent-installer, round-trip note) | `sandbox.py:1186-1192` vs `:1329-1340`; `:1211-1215` vs `:1340-1344`; `:1264-1266` vs sandbox_stop schema `:1373-1381`; `:1201-1210` vs `_dialog_stall_hint` `:1102-1114`; `:1282-1283` vs `:1129-1132` | `sandbox_run`/`sandbox_stop` descriptions carry only their own contract + a pointer; environmental facts live once in the section | high |
| SYN-17 | F2-16 | Windows-sandbox containment guidance tripled (guidelines vs sandbox section vs computer-use section) + toolkit-persistence ×3; ~1.4 KB per prompt for one idea | `loop.py:391-400` (~430 B), `sandbox.py:1172-1183` (~510 B), `loop.py:262-267` (~260 B); persistence `loop.py:390-391` + `sandbox.py:1176-1183` + `:1196-1205` | Sandbox section is the single home; one pointer sentence in guidelines; computer-use keeps only the mouse-forbidden clause | high |
| SYN-18 | F2-11 | The "~24 KB base prompt" folklore figure overstates the measured bare base by ~35% — future budget decisions must start from the real table (§3) | Measured: bare base 5,176 B; full default 17,785 B; max local 18,235 B; nothing in the matrix approaches 24 KB (absolute ceiling ~19.7 KB). Inventory §1 + spec carry the wrong figure | Correct inventory §1 with the measured table; keep §3 of this doc as the reference | high on measurements; origin of 24 KB unknown (probably a longer real skills index) |
| SYN-19 | F2-12 (Med), F4-11 (Low), F5-12 (Low) | HARNESS DOCS: `backend/prompt_manifests/README.md` — promised by the harness docstring (`prompt_manifest.py:31`, `:46-47`) and by tickets — does not exist; the remote-on-one-machine decision survives only inline in the docstring; also the inventory's title-prompt line range is stale (`loop.py:103-112` → actual `:79-124`) | `ls backend/prompt_manifests/` → 140 JSONs, no README (re-verified at baseline); grep: reference only in the docstring | Restore the README (remote-fixture decision, combo grammar, "never `--all` casually") or repoint the docstring; sweep inventory line refs once at synthesis | high |
| SYN-21 | F4-3 | Remote-workspace sub-agents resolve tool schemas via the legacy singleton host, not the run's workspace — prompt env line and schema list can describe two different hosts; `_resolve_tools`' `windows` param is dead | `subagents.py:405` → `:302` (`workspace=None`) → `tools.py:1495-1501` legacy fallback; env line `subagents.py:322-329`; harness can't catch it (renders with `workspace=""`) | Thread the run workspace into `_resolve_tools`; use its host for the env line; delete the dead param; add a fixture-host-owned-workspace combo | med-high |
| SYN-22 | F4-4 | explore's read-only promise is enforced only at schema-list time; no per-call check exists, so the prompt sentence is the only guard against a future allowlist/schema regression | `_resolve_tools` filter `subagents.py:311-313`; execution path `subagents.py:341-359` → `tools.py:1421+` consults only the access-mode gate; contrast parent per-turn strip `loop.py:1387-1390` | Belt-and-braces allowlist check in the sub-agent executor loop, or a harness/test assertion at execution resolution | high |
| SYN-23 | F4-5 | Built-in descriptions in the parent's index overstate/understate access: "all tools except ask_user and spawn_agent" is false (also excludes history search + all 11 computer tools); a parent may delegate GUI work and get `Unknown tool: screenshot` | `win-local-noshot.json` sections[8] from `subagents.py:267-291` + `:93-99`; exclusions `_ALWAYS_EXCLUDED`/`_COMPUTER_TOOLS` `subagents.py:76-83`; docstring states it correctly | Generate the exclusion clause from the real exclusion sets (or soften to "all workspace/host tools except …") | high |
| SYN-24 | F4-6 | Final-message guidance stated 3× inside every sub-agent prompt (~22% of general-purpose's prompt), the two near-verbatim copies already disagree (one drops "assumptions") | Definition bodies `subagents.py:99-106`/`:117-123` vs shared guidelines `:347-351` (guidelines sha-identical across both agents, 542 B) | Keep the contract once in the shared guidelines; reduce definition bodies to role/task | high |
| SYN-25 | F4-7 | Budget nudge "This is the final budgeted turn" denies the grace turn the for-else grants; convergence nudges accumulate unpruned (3-10 near-identical system messages in closing turns) | Nudges `subagents.py:425-454` (append-only `:453`), grace turn `:655-673`, docstring `:420-424` is accurate | Make the terminal nudge honest (or drop it — the grace message covers it); replace the running nudge instead of appending | high |
| SYN-26 | F4-8 | Compaction summarizer prompt-vs-consumer drift: "at most 400 words" is unenforced (clamp is 12,000 chars ≈ 8×), "JSON only" has a silent prose fallback, and "for context only" under-describes a summary the main agent is expected to act on | Prompt `compaction.py:260-266` (685 B manifest) vs clamp `:41`, `:275-299`; fallback `:292-298`; injection label `loop.py:1360-1363` | State the enforced contract in the prompt; fix the injection label ("decisions and state to continue from; full transcript preserved") | high |
| SYN-27 | F5-4 | Memory block renders its heading twice (wrapper heading + template's own `# Persistent memory` title); template detection only strips on byte-equality, so any edit reintroduces the dup | `memory.py:238-243` + `MEMORY_TEMPLATE` `:37-40` + `:114-131`, `:230-231`; rendered in `win-local-compaction.json` (1,257 B section, 21 B dup head) | Strip a leading `# Persistent memory` heading at injection, or reseed the template without it | high |
| SYN-28 | F5-5 | Oversized skill bodies truncated silently at 60,000 chars — no marker, no event, no test (siblings notes/memory both mark and are test-pinned) | `skills.py:159` (`:33`), contrast `loop.py:226` + `test_agent.py:1112-1115` and `memory.py:236` + `test_memory.py:127-128`; no test references the skill cap | Append the `…[truncated]` marker (and mirror into `load_skill`'s result) + cap-pinning test | high |
| SYN-29 | F5-7 | "Skill not found" is injected inside a wrapper that commands the model to treat the block as authoritative; the queued-message path lacks the `skill_not_found` event entirely (silent failure) | `skills.py:265-267`; turn path `loop.py:1284-1304`; queued `_apply_injected_skills` `loop.py:644-666` (no event); test pin `test_skill_invocation.py:59-74` | Emit `skill_not_found` from the queued path; exclude unknown names from the authoritative block | high |
| SYN-30 | F5-8 | MCP injection has no server-name validation: underscore collisions make advertised tools unroutable, `mcp_`-prefixed names can shadow the built-in namespace one level deep, and server-provided descriptions are uncapped (the one injected surface with no cap) | `mcp_client.py:181-187` (first-prefix routing), `:173-179` (verbatim merge), `:69-71` + `main.py:1330-1348` (no charset/prefix checks), `tools.py:1441-1444` (dispatch order), `:151-163` (no clamp) | Validate names at registration (`[a-zA-Z0-9-]`, reject `mcp_` prefix), route by longest-prefix/exact index, clamp descriptions | med-high |
| SYN-31 | F5-9 (Med), F2-9 (Low) | The two "Invoked skills" wrapper copies have already drifted: queued variant (263 B) drops the composer phrasing and the "Never tell the user an invoked skill is unavailable" clause the turn variant (435 B) has | Turn path `loop.py:1293-1304` vs queued `loop.py:657-666`; fact-base item 6.4 predicted this drift; it happened | Extract one `invoked_skills_wrapper(variant)` helper used by both sites | high |
| SYN-32 | F5-11 | Skill-index entries are unbounded in length (only `MAX_SKILLS=200` counts); a 5 KB SKILL.md description inflates every prompt, every turn, forever — while the body cap is 60 KB; sibling mechanisms cap their always-on text | `skills.py:252-253` (whitespace-normalize only), `:207-214`; notes cap 8 KB, index cap 12 KB for comparison | Clamp description (200-300 chars, matching `memory.py:110`'s convention) at parse time | high |
| SYN-48 | F3-5 | `edit_file` help doc's "copy old_text verbatim from read_file output" is a trap: read_file output is line-numbered (`%6d\t`), so pasting it verbatim guarantees "old_text not found" | `tools.py:293-299` vs line-numbered content `tools.py:1049` and the `:1107` error; the schema description (`:561-565`) is correct | Reword: "old_text must match the file bytes exactly — strip the line-number prefix read_file adds" | high |

### Low (15)

| ID | Sources | Finding | Evidence | Suggested fix | Conf. |
|---|---|---|---|---|---|
| SYN-33 | F3-15 | Lazy tier verbatim-copies schema text (3/3 computer docs checked, 2 core docs partially) — heeding the schema and calling `get_help` yields zero new bytes | `computer.py:83-86` vs `:265-273`; `:67-72` vs `:107-112`; `:73-80` vs `:143-150`; `tools.py:553-558` vs `:300-306` | Lazy tier carries only what the schema doesn't (failure modes, examples, cross-tool workflows) | high |
| SYN-34 | F3-6 | bash/powershell `timeout_seconds` doc says "default 60, max 900" but omits the silent `max(1, …)` floor | `tools.py:175-178`, `:216-219` vs `:906`, `:974`; the >900 clamp announces itself, the ≤0 clamp is silent | "Timeout in seconds (1-900, default 60)" | high |
| SYN-35 | F3-7 | `install_git`'s "shells opened before the install need a restart to see git" doesn't map to YAAH's per-call process model; likely stale, unverifiable read-only | `tools.py:228-236` vs `gitenv.py:61+`, per-call env `tools.py:29-33` | Verify once; drop or reword ("PATH is picked up per call — retry once") | medium |
| SYN-36 | F3-8 | Remote manifest counts 6 sandbox-tool bytes per stripped schema entry (name-only rows), so manifest byte totals mislead | `win-remote-compaction.json` `tool_schemas` vs strip at `tools.py:1525-1531` | Omit stripped schemas from the manifest list or mark `"stripped": true` | high |
| SYN-37 | F3-13 | "Never shell-background … wedges the session" duplicated in bash+powershell (249+185 B) and its rationale is stale vs the kill-tree executor (backgrounded children are killed at timeout, not wedged) | `tools.py:152-158`, `:196-199` vs `_kill_tree` `tools.py:831-849` | One shared sentence; reword rationale ("killed at timeout, losing their work") | high on dup, medium on stale rationale |
| SYN-38 | F3-16 | `spawn_agent` help doc "max 4" contradicts schema's "max 4 at once"; executor truth is a semaphore (5th call queues, doesn't fail) | `tools.py:347-351` vs `:677-679`; `subagents.py:49`, `:781` | Align help doc: "max 4 at once; extra calls queue" | high |
| SYN-39 | F3-17 | `memory_save` type param: enum documented, silent coercion to "project" not documented | `tools.py:774-779` vs `memory.py:150-152`; largest core schema at 1,079 B | "(default project; unknown values are coerced to project)" | high |
| SYN-40 | F3-18 | `web_fetch` "fall back … rather than retrying" vs executor's built-in three-route retry; a single delayed retry can genuinely succeed | `tools.py:420-424`, `:270-278` vs `webtools.py:225-251`, `:103-109` | "don't hammer the same URL — the tool already tries three routes; one delayed retry is reasonable, a loop is not" | medium |
| SYN-41 | F3-19 | `web_search` help doc teaches waiting out a "429" that can never reach the model; observable failures are the anomaly-modal challenge, empty page, or "search request failed" | `tools.py:265-269` vs `webtools.py:151-202` | Reword to the observable failure strings | medium |
| SYN-42 | F3-20 | `web_fetch`'s 5-image cap in "IMAGES ON PAGE" is undocumented in both tiers | Cap `webtools.py:269-270` vs schema `tools.py:432-436`, note `:274-277` | "first 5 image URLs" | high |
| SYN-43 | F2-10 | Computer-use section repeats "(see the sandbox section)" twice consecutively (editing leftover, ~30 B) | `loop.py:264-267`, byte-verified in `win-local-compaction` | Delete the duplicate parenthetical | high |
| SYN-44 | F2-14 | Env-line punctuation glitch in every win-local render: "Git Bash; POSIX shell syntax and utilities.; use commands…" (stray `.;`); remote env line composes the same note without it | `shell.py:68-69` + `loop.py:201-206` vs `remote.py:157-168`; identity section 257 B | Strip the note's trailing period inside `_shell_phrase` | high |
| SYN-45 | F2-15 | exit-plan approval flow stated in 3 places (~950 B per plan-mode prompt); no contradiction today (two agree), three independently editable strings | `loop.py:686-697`, `:716-740`, `:755-760` | Keep the full flow in the plan-mode note; one-line schema description or shared constant | high |
| SYN-46 | F5-10 | `memory_save` persists a body truncated by an undocumented formula (`MAX_MEMORY_BODY_CHARS + 2048` slack absorbs frontmatter) and reports success; `memory_read` then shows the amputated text | `memory.py:168` vs `:184`; no marker (mirrors SYN-28's pattern) | Cap the body portion explicitly, or truncate before composing and say so in the result | high |
| SYN-47 | F4-9 | Title prompt's "3-6 words, no quotes or period" is advisory only; consumer normalizes + clamps at 60 chars but never validates word count/periods | `loop.py:103-110` vs `_generate_conversation_title` `:79-124` (`:119-120`) | Enforce at clip time if the sidebar layout cares; else document as best-effort | high |

---

## 2. Cross-area duplication & contradiction matrix (merged, deduplicated)

Every row below is one logical duplication cluster; where two area reports
counted the same cluster (e.g. git-editor in both F2-7 and F3-11), it appears
once. Sizes are measured bytes from the area reports (rendered unless noted);
"where" lists every model-facing copy at baseline.

| # | Cluster (appears in one win-local prompt unless noted) | Copies | Where (file:line) | Measured bytes | Drift today? |
|---|---|---|---|---|---|
| 1 | Test-environment decision rule | 2 near-verbatim | loop.py:384-390; sandbox.py:1158-1171 | 632 / 611 (215 B core byte-identical) | **yes** (SYN-12) |
| 2 | git-editor rule *(counted by both F2-7 and F3-11 — merged)* | 3 model-facing (+1 code comment, +1 GIT_EDITOR-adjacent bullet) | tools.py:160-166; tools.py:200-208; sandbox.py:1222-1228; comment sandbox.py:407; adjacent sandbox.py:1192-1195 | ~880 B rendered (F2-7) / ~954 B source (F3-11) | **yes** — 3 wordings, facts split (SYN-13) |
| 3 | gh-CLI-over-scraping | 3 | tools.py:164-169; tools.py:203-208; loop.py:377-381 | ~220 + ~216 + ~60 B; facts only in the loop.py copy | **yes** — facts vs stub (SYN-14) |
| 4 | Sandbox containment (never host equiv / never host input / windows-mcp) | 3 | loop.py:391-400; sandbox.py:1172-1183; loop.py:262-267 | ~430 / 510 / 260 | **yes** (SYN-17) |
| 5 | Toolkit persistence / clean image (incl. row 4 overlap) | 3 | loop.py:390-391; sandbox.py:1176-1183; sandbox.py:1196-1205 | ~180 / 500 / 430 | **yes** (SYN-17) |
| 6 | windows-mcp playbook | 4 (section, schema, per-input note ×6 tools, guidelines) | sandbox.py:1229-1263; sandbox.py:1358-1364; computer.py:31-35 ×6; loop.py:395-401 | 1,173 + 334 + 1,422 (237×6) + abbreviated | **yes** (SYN-15) |
| 7 | Sandbox↔schema environmental cluster (clean-image, toolkit-PATH, dispose, silent-installer, round-trip) | 2 each ×5 sub-clusters | sandbox.py section vs sandbox_run/sandbox_stop schemas (see SYN-16) | ≈2.3 KB redundant | not yet (SYN-16) |
| 8 | exit_plan approval flow | 3 | loop.py:686-697; loop.py:716-740; loop.py:755-760 | ~500 / 430 / 150 | moderate — 2 agree (SYN-45) |
| 9 | "skipped: approval required" contract | 2 | loop.py:703-713 (note); loop.py:743-752 (in-band) | note overstates the gate | **yes** (SYN-05) |
| 10 | "Invoked skills" wrapper | 2 (turn vs queued paths) | loop.py:1293-1304; loop.py:657-666 | 435 / 263 | **yes** — clause dropped (SYN-31) |
| 11 | never-background warning | 2 | tools.py:152-158; tools.py:196-199 | 249 / 185 | rationale stale vs kill-tree (SYN-37) |
| 12 | Final-message contract (sub-agent prompt) | 3 (2 near-verbatim + 1 paraphrase) | subagents.py:99-106/:117-123; :347-349; :350-351 | ~420 of 1,881 B ≈ 22% | **yes** — "assumptions" dropped (SYN-24) |
| 13 | Delegation policy (parent-side) | 3 | subagents.py:276-291; tools.py:655-673; tools.py:342-350 | 1,310-B section + schema + help | agree today, 3 phrasings (SYN-09) |
| 14 | Prose tool list vs real schema list | 2 hand-maintained (parent + sub-agent) | loop.py:327-369; subagents.py:330-339 | 768 / 347 | **yes** — drifted both directions (SYN-09, SYN-02) |
| 15 | Lazy-tier docs vs schemas (verbatim) | 2 each | computer.py help×3; tools.py read_file etc. | see SYN-33 | verbatim today; zero value added (SYN-33) |
| 16 | Memory heading | 2 in one block | memory.py:238-243 + MEMORY_TEMPLATE:37-40 | 21 B + blank line | **yes** — structural (SYN-27) |
| 17 | windows_bash_note composition (local vs remote env line) | 2 | loop.py:186-207; remote.py:123-173 | — | punctuation drift (SYN-44) |

**Duplication tax (net recoverable, keep-one-copy estimates per win-local
prompt):** git-editor ~0.6 KB · gh-CLI stubs ~0.44 KB · containment ~0.9 KB ·
windows-mcp ~1.7 KB · sandbox↔schema clusters ~1.8 KB · test-env rule ~0.6 KB ·
exit-plan ~0.4 KB · final-message (per spawn) ~0.3 KB. **Total ≈ 6.7 KB of
measured redundancy in the default local prompt (~38% of the 17.8 KB base)** —
before counting the wrong-platform guidance SYN-07 makes dead weight in
posix/remote renders (~2.6 KB of guidelines).

## 3. Token economy (manifest `total_bytes` at `fb85de2`)

| Combo | Total bytes | Largest sections |
|---|---|---|
| win-local-plan-compaction | 18,235 | windows-sandbox 6,749 · guidelines 2,692 · computer-use 2,610 · subagent-index 1,310 · tools 780 · spoken-briefing 758 · ask-user 689 |
| win-local-compaction (full default) | 17,785 | same shape (−plan note, −exit_plan schema); bare base subtotal (identity+env+tools+guidelines+briefing+ask-user) = **5,176** |
| win-local-noshot | 17,773 | guidelines 2,692 · windows-sandbox 6,749 · computer-use 2,610 · persistent-memory 1,242 · subagent-index 1,310 |
| win-local-override-compaction | ≈9.7 KB | override head replaces the whole base; fragments/indexes still appended |
| win-remote-compaction | 7,809 | guidelines ≈2.6 KB · subagent-index 1,310 · identity ≈370 (schemas 12,236 B / 20 tools) |
| win-remote-offline-plan | 16,293 | byte-identical to `-normal` (SYN-06) |
| posix-local | 6,699 | guidelines ≈2.6 KB — carries Windows guidance it cannot act on (SYN-07) · subagent-index 1,310 |
| Sub-agent prompts (per spawn batch) | 1,881 + 1,818 = 3,699 | subagent-base 633/570 · tools 347 · guidelines 542 · skills-index 352; ~28% exact-line overlap with parent lines, mostly the shared skills index (per-spawn, not per-turn) |

Reading: the sandbox section alone is 37% of the default local prompt; the
always-on injected baseline (notes + memory + one skill) adds ~2.9 KB of which
~1.6 KB is fixed wrapper/guidance text; the unbounded always-on costs are the
skill-index descriptions (SYN-32) and the memory index (capped 12 KB, marked);
the unbounded per-turn costs are attachment `content` (SYN-11) and MCP
descriptions (SYN-30). Schema-set sizes: win-local 35 tools / 25,482 B;
win-remote 20 / 12,236 B; posix 19 / 11,075 B.

**Correction of record:** the "~24 KB base prompt" figure in the spec and
inventory §1 is wrong by ~35% — the measured bare base is **5,176 B** and the
largest observed combo is 18,235 B (SYN-18). The fixture skills index is 354 B
with two one-line skills; a real 34-skill install will be several KB
(inventory estimates 4-5 KB, unmeasured by the harness).

## 4. Contradictions between area reports (resolved)

| Disagreement | Areas | Resolution |
|---|---|---|
| Phantom git tools severity | F3-2 High vs F4-1 Critical | **Critical** (SYN-02). F4-1's argument stands: the sub-agent's own prompt advertises them on every run in all four manifest variants, the call fails deterministically mid-task, and the remote-forward escape hatch is a dead end (host dispatches the same executor map). |
| git-editor rule size | F2-7 "~880 B total" vs F3-11 "~954 B total" | Same cluster, different measurement basis: F2-7 measured rendered prompt bytes (372 B sandbox copy), F3-11 measured source literals (438 B sandbox copy). Both plausible; counted **once** in the merged matrix (row 2). |
| windows-mcp note attach count | inventory lead "7 tools" vs F3-9 "6" | **6** (F3-9 refined): `focus_window` is input-adjacent without the note. Spot-checked at baseline: `_HOST_INPUT_NOTE` = 1 definition + 6 attach sites in computer.py. |
| Compaction harness gap severity | F2-13 Medium vs F5-1 High | **Recorded High** (SYN-20): the manifest for `-compaction` combos silently lacks the fragment the flag exists to exercise — a review-evidence gap in the instrument every other finding relies on. |
| Invoked-skills wrapper drift severity | F2-9 Low ("no contradiction, cosmetic") vs F5-9 Medium | **Medium** (SYN-31): F5-9 showed the drift has already happened (clause dropped), which is the definition of Medium on the spec scale. |
| gh-CLI duplication severity | F2-8 Medium vs F3-12 Low | **Medium** (SYN-14): the schema copies are normalized-identical to each other (F3-12), but only the guidelines copy carries the substantive facts (F2-8) — facts-vs-stub is drift risk. |
| Sub-agent memory tools severity | F5-2 self-flagged "High, Critical defensible" | **High** (SYN-10): the claim-vs-assembly contradiction is real and reachable on every spawn; kept at High because the tools themselves work and the failure is silent confusion rather than a wrong action. Re-openable by the owner. |

## 5. Harness fidelity — fix-forward candidates

These are defects in the #161 manifest harness (and its docs) found by the
review itself. They are findings in their own right — the harness is the
instrument the whole review depends on, and two of them mean some committed
evidence does not model the real prompt:

1. **SYN-20 (F2-13/F5-1): the `-compaction` combos test nothing.** The bootstrap
   warm-up consumes the fixture watermark, `compact_conversation` silently
   rejects the stale watermark, and all 33 committed `-compaction` manifests
   lack the compaction-summary section that `SECTION_OPENINGS` anticipates. The
   summary-injection path has zero rendered-byte coverage. Fix the fixture and
   add a self-check that fails loudly when an expected section is absent.
2. **SYN-49 (F5-3): sub-agent manifests render with an empty workspace**, so the
   AGENTS-notes injection the production builder performs is invisible in the
   committed evidence (`has project-notes: False` while real sub-agents in a
   workspace with AGENTS.md receive the block). Render against the fixture
   workspace instead.
3. **SYN-19 (F2-12/F4-11/F5-12): `backend/prompt_manifests/README.md` went
   missing during regeneration and must be restored** (or the docstring pointer
   fixed) — the harness docstring and review tickets both promise it; the
   remote-on-one-machine decision currently survives only inline in the
   docstring. Also fold in the inventory line-ref refresh (title prompt
   `loop.py:103-112` → `:79-124`) and the manifest accounting nit F3-8
   (stripped schemas listed at 6 B each in remote manifests).

## 6. Not actionable / verification record (every input finding accounted for)

- **F3-21 (verification record, no finding).** In-band strings verified
  consistent, checked one by one: timeout-clamp note, `get_help` nudge + error
  nudge, screenshot-disallowed info, plan-block result, sandbox-only skip
  result, `_missing_command_hint`, `wait`/`press_key`/`mouse_drag` caps,
  `search_files`/`search_conversation_history` caps, sandbox boot/timeout
  descriptions. Full evidence table in the area-3 report. Two minor
  undocumented items found during the sweep were folded into SYN-34 (the ≤0
  clamp) and SYN-42 (5-image cap); `ask_user`'s unvalidated "2-4 options" and
  `read_ui_tree`'s undocumented clamps were noted as harmless and **not**
  carried forward.
- **Review 2 — fragment trigger verification table:** all conditional fragments
  verified present/absent against renders (computer-use, sandbox, screenshot
  prose, plan note + exit_plan, sandbox-only note, override, skills index,
  memory block, offline note); two anomalies became SYN-06 and SYN-20; the
  invoked-skills wrapper is a known matrix gap (no combo invokes a skill).
- **Review 3 — tool-coverage table:** 35+ tools checked
  schema-vs-executor-vs-lazy-tier-vs-in-band; the consistent majority are
  recorded there and produced no findings beyond those listed in §1.
- **Review 4 — auxiliary contract table + budget quick reference:** verified
  consistent items (compaction merge-with-earlier-summary honored; title
  regeneration guard matches docstring; grace-turn message honest; "N turns
  remain" honest) — only the drift items became findings.
- **Review 5 — mechanism questions answered:** outside-authored labeling honest
  in every wrapper; caps real where claimed except SYN-11/SYN-28/SYN-30/SYN-32;
  silent-vs-loud failure modes correct for optional context, malformed
  SKILL.md, unreachable MCP servers; notes-wrapper "override the general
  guidance below" positionally accurate in both parent and sub-agent
  assemblies.
- **Considered, deliberately not filed (synthesis judgment):** review 4's
  observation that sub-agent prompts omit gh-CLI guidance, a commit-policy
  line, and an edit-vs-write preference. These are one-line addition
  candidates, not contradictions; left to the owner's judgment alongside the
  SYN-09 consolidation, where the tool-list helper would make adding them
  cheap.
- **Non-actionable corrections owned by this document:** the 24 KB folklore
  figure (corrected in §3, SYN-18) and the stale inventory line reference
  (folded into SYN-19) need no separate fix work beyond the doc updates
  already tracked by their SYN rows.

## 7. Disposition

- 49 consolidated findings → **26 actionable GitHub issues** (batch preserved in
  `C:\Users\Administrator\YAAH\.scratch\findings-issues\`), each labeled
  `ready-for-agent`, each linking parent spec #160.
- 0 findings dropped: every one of the 59 source findings maps to a SYN row,
  and every SYN row maps to an issue or to §6 above.
- The review itself changed no code; fixes flow from the issues.
