# Ponytail audit 1/4 — backend/agent/ (issue #269)

- Scope: `backend/agent/` ONLY (~16.7k LOC, 36 modules). Out of scope per #269: tests, `bundled_toolkit/`, `bundled_skills/`, `data/`, `installers/`, `prompt_manifests/` data, and correctness/security/performance.
- Method: four parallel read-only sweeps (agent core; sandbox/remote/git plumbing; voice/web/MCP/memory; cross-cutting duplication + dead exports). Every dead/unused claim repo-wide-grep-verified; maintainer spot-checked the four largest delete claims.
- Format: one line per finding, ranked biggest cut first. List only — nothing applied.

## Findings

- `shrink:` subagents.py `run_sub_agent` runs the whole per-tool-call block twice — main turn loop (599–685) and again nearly verbatim in the for-else grace turn (744–811), plus a duplicated stream-consumption event loop (527–543 vs 708–724). Extract `_execute_tool_calls(...)` + `_consume_stream(...)` helpers, call from both. [backend/agent/subagents.py] (~70 lines)
- `shrink:` sandbox_preview.py follows the host twice: a WinEvent LOCATIONCHANGE hook (WINEVENTPROC typedef, `_win_event_proc`, `_install_follow_hook`, unhook cleanup) plus a guaranteed 60ms `_follow_and_confine()` tick in the pump loop; the code's own comments call the hook "opportunistic". Keep the tick, delete the hook. [backend/agent/sandbox_preview.py] (~45 lines)
- `shrink:` loop.py repeats the identical 9-line file-changes finalize block (emit → persist → yield `file_changes`) at 7 sites (1886–1894, 2043–2051, 2159–2167, 2198–2206, 2616–2622, 2651–2657, 2667–2673). One `async def _finalize_file_changes(...)` helper. [backend/agent/loop.py] (~35 lines)
- `dedupe:` frozen-exe/bundle-dir candidate scans re-implemented per module (`exe`, `exe/_up_`, `Path.cwd()` + `_up_/backend/...` walks) plus identical `_exe_dir()` frozen/dev helpers in three modules. One `exe_dir()` + `bundle_dirs(*parts)` helper in a shared resources module (gitproc.py is the existing dependency-light plumbing home). [backend/agent/gitenv.py, transcribe.py, speak.py, skills.py, sandbox.py] (~35 lines)
- `dedupe:` `_run_git` hand-rolled a third time in file_changes.py (spawn flags, timeout-kill, bytes return); gitinfo's `_run_git` is already the shared home (imported by runwatch, worktrees, wt_sweep, tools, main). Reuse it (or move it to gitproc). [backend/agent/file_changes.py:22–52 vs backend/agent/gitinfo.py:123–153] (~20 lines; sweep 2 separately counted file_changes' own `_run_git` as a yagni cut — counted once here at the dedupe value)
- `shrink:` sandbox_preview.py declares argtypes/restype for ~30 Win32 functions where ctypes is already safe — pointer-only/32-bit-arg functions (PeekMessageW, TranslateMessage, DispatchMessageW, RegisterClassExW, GetCursorPos, ReleaseCapture, GetSystemMetrics) pass undeclared, `SetProcessDPIAware` is declared but never called, and an ignored restype is unnecessary. Keep declarations only where 64-bit handle truncation is real (HWND/HANDLE args). [backend/agent/sandbox_preview.py] (~25 lines)
- `native:` sandbox_preview.py re-declares `POINT`, `RECT`, `MSG` as local ctypes Structures while `ctypes.wintypes` (already imported and used for `wintypes.RECT` elsewhere in the file) ships all three with identical layouts. [backend/agent/sandbox_preview.py] (~20 lines)
- `dedupe:` remote_runner.py `_history_from_snapshot` re-implements loop.py `load_history`'s replay rules (orphan tool-row drop, tool_call validity sets, images→parts lists). Extract one shared pure helper; the runner keeps only its no-DB/no-attachments delta. [backend/agent/remote_runner.py:129–183 vs backend/agent/loop.py:1425–1514] (~25 lines)
- `yagni:` sandbox_preview.py `sandbox_test`'s `timeout_seconds` tool parameter is validated then discarded — the boot deadline comes exclusively from `sandbox.startup_timeout` config; the schema advertises a knob that does nothing. Remove from SANDBOX_TOOLS_SCHEMA + validator. [backend/agent/sandbox.py] (~15 lines)
- `yagni:` remote_turn.py `append_message`/`update_message` (+ `_invalidate_pending_commit` triggers) — production commits via the runner's `_InMemoryTranscript`; only tests call these mutators. Fold into the test fixture or delete. [backend/agent/remote_turn.py] (~17 lines)
- `dedupe:` byte-identical `_yaml_load` (safe_load + flat `key: value` fallback) and `_FRONTMATTER_RE` duplicated into subagents.py. Keep skills' copy; subagents imports it. [backend/agent/skills.py:31,187–200 vs backend/agent/subagents.py:42,149–160] (~16 lines)
- `dedupe:` Windows no-console/new-session subprocess flag pairs declared independently in five modules (`_NO_WINDOW`/`_NEW_SESSION`, `_CREATE_NO_WINDOW`, gitproc.NO_WINDOW/NEW_SESSION). One shared pair; gitproc's is currently the orphan. [backend/agent/gitproc.py, gitinfo.py, file_changes.py, tools.py, transcribe.py, sandbox.py] (~14 lines)
- `dedupe:` git-executable discovery twice — `gitenv.find_git()` is bare `shutil.which("git")` plus its own Program Files candidates; `gitproc.git_exe()` does which + .cmd-shim probes with the same candidate list. gitenv delegates to gitproc. [backend/agent/gitenv.py:27–29,94–100 vs backend/agent/gitproc.py:29–52] (~12 lines)
- `yagni:` compaction.py knobs `keep_fraction`, `keep_recent_messages`, `default_window` (+ their clamp try/excepts at 71–86) are read from config `compaction` but nothing repo-wide sets them — Settings API persists only `enabled`/`trigger_tokens` (main.py:2424–2433), frontend type carries only those. Hard-code the constants (they already exist as COMPACTION_*). [backend/agent/compaction.py] (~15 lines)
- `shrink:` loop.py carries two copies of the `<say>`-tag stream scrubber: the while-loop tag splitter in `_model_step` (1910–2017) and the EOF fallback re-scan (2090–2116 path). One scrubber generator fed by both. [backend/agent/loop.py] (~30 lines)
- `shrink:` loop.py `_selected_branch_note` / `_selected_branch_note_degraded` / `_selected_branch_note_stale` (921–1073) repeat header line, origin ternary, worktree instructions, residue tail three times with per-variant prose. One function with a variants table. (Note: deliberate per-state wording was a recorded decision #301/#302 — a table can keep the wording per state; cut is the shared framing, so real saving may land lower.) [backend/agent/loop.py] (~30 lines, may land ~15)
- `shrink:` sandbox_preview.py `_find_sandbox_window` hand-probes each top-level window's process image via OpenProcess/QueryFullProcessImageNameW declarations + handle dance; the codebase already parses `tasklist /FO CSV /NH` for exactly this (`_sandbox_pids`). Filter EnumWindows by that PID set, drop the kernel32 declarations. [backend/agent/sandbox_preview.py] (~15 lines)
- `native:` sandbox.py `_acquire_toolkit_manifest_lock`/`_release_toolkit_manifest_lock` hand-roll a named cross-process mutex via ctypes kernel32; `msvcrt.locking` (LK_NBLCK) on a toolkit lockfile is the platform primitive for the same job in a fraction of the code. [backend/agent/sandbox.py] (~12 lines)
- `delete:` tools.py `_git` (1405–1429) — zero callers repo-wide (branch_select uses `gitinfo._run_git`). [backend/agent/tools.py:1405–1429] (~25 lines)
- `delete:` skills_ledger.py — the whole module (68 lines) has zero non-test callers (only `backend/tests/test_skills_ledger.py` + a docs mention as the #57 "Phase 3 seed" for a skill-manager panel that doesn't exist); `load_bundled_ledger` is a pure alias. Listed here at maintainer's discretion since tests-only is technically a keep per the repo rules — top whole-module delete candidate. [backend/agent/skills_ledger.py] (~68 lines)
- `delete:` remote_runner.py dead code: no-op `if name in remote_mod.REMOTE_TOOLS ... pass` block (457–460), never-read `holder_host_id` param of `_commit_via_owner`, `state["finish"]` set and never read. [backend/agent/remote_runner.py] (~8 lines)
- `yagni:` gitenv.py `run_install_git` re-rolls case-insensitive PATH-key lookup + dedupe-prepend that ghenv.command_env (and shell.resolve_git_bash:59) already own. Reuse ghenv's helper. [backend/agent/gitenv.py:103–109 vs ghenv.py:46–51] (~9 lines)
- `delete:` gitproc.py NO_WINDOW/NEW_SESSION constants + unused `import subprocess` — zero consumers (every module re-rolls its own; see dedupe finding above, which is the fix if constants are kept). [backend/agent/gitproc.py] (~6 lines + 1 import)
- `delete:` prompt_manifest.py dead bits: `_isolated_roots()` (never called, ~9 lines), `_local_flags()` (never called; `_iter_local` hardcodes the same six flags, ~4 lines), `_prep_flags(flags, tmp)`'s `tmp` param always None, `BASE_JOIN` constant never used. [backend/agent/prompt_manifest.py] (~15 lines)
- `native:` memory.py `_entry_type` hand-rolls a YAML frontmatter line-parser (~16 lines of indent tracking); pyyaml is a declared dep and skills.py already parses frontmatter with `yaml.safe_load`: `meta = yaml.safe_load(fm.group(1)) or {}; t = (meta.get("metadata") or {}).get("type")`. [backend/agent/memory.py] (~15 lines)
- `shrink:` transcribe.py `_binary_dirs`/`_model_dirs`/`find_binary`/`find_model` (52 lines) → one `_first_file(dirs, names)` helper over the existing dir lists; the same shape recurs in speak.py `_model_dir_candidates` and gitenv.py (see dedupe finding above). [backend/agent/transcribe.py] (~12 lines)
- `shrink:` loop.py `_await_approval` (1177–1204) re-implements `_wait_answer`'s future/cancel/asyncio.wait machinery (655–677) line for line. Call `_wait_answer`, map its result to approve/deny/freetext. [backend/agent/loop.py] (~15 lines)
- `shrink:` loop.py gate-re-exec branch (2340–2359) duplicates the direct-exec `_execute_with_progress` block (2296–2308) including a doubled `if result is None:` guard. One execute call after the gate. [backend/agent/loop.py] (~13 lines)
- `shrink:` tools.py run_bash vs run_powershell (1128–1201) duplicate spawn/capture/truncate plumbing (~40 lines of near-identical code differing in shell invocation) — one parameterized spawner. [backend/agent/tools.py:1128–1201] (~20 lines) *(file-local duplication noted by sweep 4, merged from Notes)*
- `shrink:` three policy `_sub_gate` closures (2487–2507) — each a lambda-shaped def returning `asyncio.sleep(0, result=...)` with a `# type: ignore` — are one async function with two early returns. [backend/agent/loop.py] (~10 lines)
- `delete:` providers.py `detect_preset` (44–50) — zero callers repo-wide (PRESETS itself is served by /api/providers — keep the dict, cut only the function). [backend/agent/providers.py] (~7 lines)
- `delete:` shell.py `windows_bash_kind` — zero callers anywhere, including tests (confirmed by both sweeps). [backend/agent/shell.py:63–64] (~4 lines)
- `yagni:` subagents.py `AgentDef.model` field — "parsed but deferred (v1.1)", written by parse_agent_md (207), never read anywhere. Field + parse line go. [backend/agent/subagents.py] (~2 lines)
- `yagni:` loop.py `_resolve_max_steps` legacy per-provider fallback `prov.get("max_steps")` (286–287) — nothing writes `providers.<name>.max_steps` (Settings writes `model_steps`; remote_runner reads only the global). [backend/agent/loop.py] (~2 lines)
- `yagni:` skills.py `_yaml_load` ImportError fallback (hand-rolled flat `key: value` parser) — pyyaml is a hard dep; when yaml is missing the app is broken anyway. Drop fallback, keep `yaml.safe_load` (folds into the `_yaml_load` dedupe above). [backend/agent/skills.py] (~2 lines, subagents mirror +2)
- `yagni:` speak.py `_BRIEFING_MAX = SAY_MAX_CHARS` — alias indirection through which every `max_chars: int = _BRIEFING_MAX` default actually reads SAY_MAX_CHARS. One name. [backend/agent/speak.py] (~2 lines)
- `shrink:` web_fetch's three copy-pasted try/strip/check-marker route blocks → loop over `((_browser_get, "Chrome"), (_curl_get, "curl"), (_http_get, "direct"))` with the marker check as break condition. [backend/agent/webtools.py] (~5 lines)
- `shrink:` mcp_client `_session_loop`'s ExceptionGroup leaf-walk recursion → `state.error = "; ".join(str(leaf) for leaf in eg.exceptions)` — ExceptionGroup's own str already renders nested leaves, and the message is display-only. [backend/agent/mcp_client.py] (~5 lines)
- `stdlib:` two hand-rolled WAV writers with identical `wave.open/setnchannels(1)/setsampwidth(2)/setframerate/writeframes` bodies: speak.wav_bytes (in-memory) and transcribe.save_wav (temp file). save_wav wraps wav_bytes + `NamedTemporaryFile(suffix=".wav", delete=False)`. [backend/agent/transcribe.py, backend/agent/speak.py] (~5 lines)
- `delete:` speak.py `normalize_prose_for_speech` — zero callers (composition in spoken_line/loop.py calls `normalize_for_speech` directly). [backend/agent/speak.py] (~3 lines)
- `delete:` speak.py `strip_say_tags` — public one-line re-export of `_strip_say_tags`, only caller is a test; extract_say is the production API. [backend/agent/speak.py] (~3 lines)
- `shrink:` webtools `_find_chrome`'s four hardcoded `C:\Program Files*` candidates → `shutil.which("msedge.exe")` covers both Program Files variants; keep `shutil.which("chrome")` + the chrome paths. [backend/agent/webtools.py] (~3 lines)
- `shrink:` attachments.py `inline_attachment_text`'s byte-length char-shaving loop (`while len(cut.encode()) > LIMIT: cut = cut[:-1]`, O(n) re-encodes of a 100KB string) → `content.encode("utf-8")[:INLINE_LIMIT_BYTES].decode("utf-8", errors="ignore")`. [backend/agent/attachments.py] (~2 lines)
- `shrink:` mcp_client `clamp_description` → `return t if len(t) <= _MAX_DESC_CHARS else t[:_MAX_DESC_CHARS] + "…[clamped]"`. [backend/agent/mcp_client.py] (~1 line)
- `shrink:` file_changes.py `diff_snapshots` is an async function with no await (pure dict diff); make it sync like the `_filesystem_diff` it wraps. [backend/agent/file_changes.py] (~2 lines)
- `delete:` webtools.py `import base64` at 290 — imported, never used in that function. [backend/agent/webtools.py:290] (~1 line)
- `delete:` speak.py `_download_model_inner` re-imports httpx (line 202) already imported at module level (line 26). [backend/agent/speak.py:202] (~1 line)
- `delete:` tools.py module-level `from backend.agent.ghenv import command_env` (line 18) dead — only the local re-import inside `_tool_env` (line 27) is used. [backend/agent/tools.py:18] (~1 line)
- `delete:` tools.py `import shlex` (line 13) — unused in file. [backend/agent/tools.py:13] (~1 line)
- `shrink:` loop.py nested duplicate `if result is None:` guard at 2340–2341 — one suffices (folded into the gate-re-exec finding above; counted once). [backend/agent/loop.py] (~1 line)
- `delete:` prompt_manifest.py `BASE_JOIN` constant — never used (folded into the prompt_manifest dead-bits finding; counted once). [backend/agent/prompt_manifest.py] (~1 line)

## Sweep-attributed nets (pre-dedupe)

- Sweep 1 (agent core, ~6.7k LOC read): net -244 lines
- Sweep 2 (sandbox/remote/git, ~5.4k LOC read): net -209 lines, -1 dep
- Sweep 3 (voice/web/MCP/memory, ~4.1k LOC read): net -144 lines
- Sweep 4 (cross-cutting, whole scope): net -180 lines

## Net for this scope (backend/agent/)

`net: -769 lines, -0 deps possible`

57 merged findings above sum to 777; less 8 lines of non-additive overlap = 769. The subtractions: loop.py's
duplicate `if result is None:` guard (~1) and prompt_manifest.py's BASE_JOIN (~1) are folded into their parent
findings, and gitproc.py's NO_WINDOW/NEW_SESSION cut (~6) is the alternative to the NO_WINDOW dedupe finding —
adopting the dedupe keeps gitproc's pair as the canonical home instead of deleting it. Also non-stacking:
file_changes' `_run_git` is counted once (at the cross-sweep dedupe value), and the `_yaml_load` fallback cut
stacks intentionally on top of that dedupe (dedupe removes the duplicate copy; the yagni shrinks the surviving one).
Deps: none — every third-party import in scope is used, and every requirements.txt entry is justified; sweep 2's
"-1 dep" was the ctypes-mutex → msvcrt.locking swap, stdlib-to-stdlib, not a packaging change.

## Notes (merged, unflagged borderline calls)

- Tests-only symbols (privatize, not dead, per rules): `tools.SCHEMAS`, `loop.try_begin_run`, `remote.set_remote`, `compaction.estimate_tokens/should_compact/find_cut_index/resolve_window`, `sandbox.sandbox_available()`, preview `gesture_hit_test()`, `runwatch.invalidate_run_caches`.
- `skills_ledger.py` whole-module delete is recorded above as the top candidate despite the tests-only keep rule.
- `prompt_manifest.py` (1082 LOC) is a test-harness/CLI living in the prod tree; it's the documented #161 mechanism with committed fixtures — only its dead bits were flagged, not the module.
- webtools' Chrome→curl_cffi→urllib fallback chain is documented deliberate design — not flagged; only its copy-paste shape and two dead lines.
- speak.py's ~350-line number/SSML normalization engine (875–1138) mirrors src/speech.ts with pinned shared test cases (SHARED_CASES) — deliberate cross-platform contract, not flagged.
- Windows Job Object ctypes block (tools.py:43–108): no stdlib equivalent for kill-on-close job semantics; psutil not a dep — not flagged.
- model_client `_stream_response` legacy config-fallbacks stay: backend/agent/remote_runner.py calls it directly.
- The asyncio.wait + queue-get race pattern appears ~6× across loop.py/subagents.py with different cancellation semantics per site — merging judged riskier than the ~30-line saving; not flagged.
- worktrees.py vs wt_sweep.py NOT duplicates (run-end retirement vs hourly sweeper; deliberately share `chat_worktree_has_run_tree`). ghenv vs gitenv: distinct features; only git discovery overlaps (flagged).
- mcp_client's dual-generation getattr shims (input_schema/inputSchema etc.) are SDK-version compatibility; `create_mcp_http_client` import-guard mirrors them — not flagged.
- `memory.py _case_insensitive_fs()` is a deliberate test seam (patching os.name breaks pathlib on CI) — kept.
- `discovery.browse`'s ServiceBrowser + sleep sweep matches zeroconf's documented example pattern; preview's ctypes handle discipline is justified where 64-bit truncation is real; `_seed_locked`'s Python INDEX.md renderer mirrors the toolkit's PS renderer by design (PS call would be slower). All kept.
- `RemoteSession.host_id` hostname fallback + `register_remote`'s legacy getattr shim are pre-host_id compat paths the v5 protocol gate likely makes unreachable — noted, not provable from code alone.
- `sandbox_run/status/stop` ignore their `workspace` param — interface-mandated by the uniform executor signature in tools.py, not flaggable.
- context_window `_KNOWN_WINDOWS` is a data table, not logic; trimming stale model rows is maintenance, not an over-engineering cut.

*Generated by ask-matt run for issue #269 (backend/agent/ pass 1 of 4). Findings only — nothing applied.*
