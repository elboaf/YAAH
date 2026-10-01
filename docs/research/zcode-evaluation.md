# ZCode evaluation: computer-use + built-in browser as borrow sources

**Issue:** #170 · **Evaluated:** 2026-10-01 · **Source:** [zai-org/ZCode](https://github.com/zai-org/ZCode) v3.14.3 (Apache-2.0), cloned at HEAD `29628c9` during this evaluation.

> This is `zai-org/ZCode` (Z.ai's GLM harness) — not the unrelated `zerx-lab/zcode` terminal agent.

**Scope:** research only — no code adopted in this task. Findings feed the follow-up grill-with-docs → spec flow.

**Repo caveat (confirmed):** the public repo is a source dump — 3 commits, single tag. There is no upstream history to mine for rationale. More importantly, the actual computer-use **executor was never open-sourced**: `packages/zcode-cua` is an explicit fails-closed placeholder (`packages/zcode-cua/index.js` returns *"Computer Use is not available in this build"*; its `package.json` self-describes as an "API-compatible placeholder… fails closed"). What the repo offers is contracts, protocol, policy, and plumbing — plus a fully present browser-use stack.

---

## 1. Computer-use: architecture and portability

### How it works

- **Plugin packaging.** CUA is a built-in plugin `computer-use` defined in `apps/zcode-cli/packages/bootstrap/src/app/official-plugin-definitions.ts` (bundling `docs/computer-use.md`, a client script, and a `computer-use` skill). Enabling it sets `runtimeFeatures.computerUse` (`bootstrap/src/app/plugin-runtime-features.ts`) and requires a product helper key.
- **Execution via code cells, not discrete tool calls.** The model writes JavaScript into `mcp__node_repl__js` cells; every CUA cell must bootstrap with `setupComputerUseRuntime(...)` first — an invariant documented at `apps/zcode-cli/packages/bootstrap/src/zcode-protocol/computer-use-operation-event.ts` (each cell is a fresh worker; SDK bindings don't span cells).
- **Bridge/broker process isolation.**
  - `apps/zcode-cli/packages/node-repl-host/src/cua-bridge.ts` (204 lines): injects a per-cell bridge global; blocks subagents (both here and at `core/src/subagent/computer-use-policy.ts` — defense-in-depth); reads the primary target app from the *trusted broker response*, never from model-writable output. Transport: newline-delimited JSON over a local socket, token-authed, 32 MiB response cap.
  - `apps/zcode-cli/packages/node-repl-host/src/cua-broker.ts` (181 lines): local server on a **Windows named pipe** (`\\.\pipe\zcode-node-repl-cua-<uuid>`) or Unix socket; one request per connection; 32-byte hex token compared with `timingSafeEqual`; 1 MiB request cap; dispatches to `ComputerUseRuntime.execute(...)` — the closed producer's interface (`packages/zcode-cua/index.d.ts`).
- **Tool surface (26 tools):** `request_access, list_apps, list_windows, get_app_state, screenshot, zoom, open_application, left/double/triple/right/middle_click, scroll, left_click_drag, mouse_move, type, set_value, select_text, key, hold_key, perform_action, wait, read/write_clipboard, stop_computer_control` (`packages/ui/src/ToolCallBlocks/renderers/cuaSummaryMessages.ts`). Targets are element indices from `get_app_state` snapshots or raw coordinates.
- **Permission model.** `request_access` returns a strict status `{schemaVersion, platform, grantOwner, accessibility, screenRecording}` (`packages/shared/src/zcode-protocol-v4/cuaPermission.ts`). macOS: full TCC guided onboarding (`packages/desktop/src/main/cuaAccessibilitySettings.ts`, 646 lines, with drag-in TOCTOU protection via bundle fingerprints). **Windows: no OS permission dialogs** — the settings section states Windows reuses only the plugin master switch (`packages/ui/src/settings/ComputerUseSection.tsx`); availability marks `local-windows` supported (`packages/ui/src/settings/computerUseAvailability.ts`). The Windows helper is an integrity-checked (manifest + SHA-256) forked child process with a two-phase shutdown lifecycle (`packages/services/src/cua-permission-broker/windowsCuaDevRuntime.ts`, `windowsCuaHelperHostSupport.ts`, `windowsCuaDevHelperHost.ts`).
- **Observation loop and stale-raster guard.** Screenshots return image + imageRef credential pairs with frame attestation contracts (`packages/zcode-cua/frame-contract.d.ts`). When a raster is dropped from model context, a text substitute tells the model *"Do not send a coordinate target; capture a new raster first"* (`apps/zcode-cli/packages/core/src/runtime/helpers/official-cua-media.ts`) — a cheap, correctness-critical pattern.
- **Operation event protocol.** Session events map to a small discriminated union (`turn-started/completed/failed`, `tool-scheduled` (+CUA flag), `tool-started`, `session-closed`) — `computer-use-operation-event.ts`; protocol-v4 adds a `list_apps` snapshot parser (`zcode-protocol-v4/cua-app-snapshot.ts`) and permission-observation normalizer (`cua-permission-observation.ts`).

### Portability split

| Class | Contents |
|---|---|
| **OS-portable** (~600 lines of pure logic) | Operation-event mapper, permission schemas + normalizer, app-snapshot parser, `kind:"cua"` display contract, stale-raster guard, subagent CUA policy, availability matrix. Plain data transforms — trivially re-expressible in Python/pydantic. |
| **OS-specific but analogous to Windows UIA** | The broker/bridge pair (named-pipe, token auth, one-shot connections, trusted `_meta` app associations) is exactly the seam between an isolated agent context and a privileged desktop driver. The Windows helper lifecycle (process generations, shutdown→kill→blocker) matters only if YAAH drives the host desktop from an out-of-process driver. |
| **Desktop-coupled — skip** (~4,500 lines) | All macOS TCC machinery, PiP/focus routing, and the React renderers as code (~2,500+ lines; their display *schema* is worth copying). |
| **Never open-sourced** | The AX/UIA executor itself. YAAH must build its own pywinauto/UIA + capture implementation regardless; only the contracts transfer. |

---

## 2. Built-in browser: architecture and portability

### How it works

- **Chain:** model → `mcp__node_repl__js` (fresh kernel per call; **browser tabs are the continuity boundary**) → browser-client SDK (`agent.browsers` facade, `apps/zcode-cli/packages/core/src/browser-client/`) → `BrowserClientTransport` → **`BrowserControlPort`** contract (`apps/zcode-cli/packages/contracts/src/interfaces/browser-control.port.ts`, 545 lines) → backend adapter → Playwright (headless CDP) or Electron WebContentsView (desktop panel).
- **Port contract.** `list / execute / turnEnded? / closeSession?` plus a 40+-method `BrowserCommand` union (navigate, snapshot, click/fill/type/press, `cua*` coordinate and `domCua*` node-id escape hatches, screenshot, evaluate, dialog handling, tab lifecycle: `newTab/listUserTabs/claimTab/finalizeTabs`). Encodes cancellation, stale-generation rejection, and `sideEffect: "uncertain"` reporting for cancelled side-effectful commands.
- **Managed CDP adapter** (`apps/zcode-cli/packages/adapters/src/browser/`, 8 files ≈ 1,550 lines): lazily launches one headless Playwright Chromium (`--no-first-run --no-default-browser-check`), 1280×720 viewport, downloads disabled; a generation counter invalidates stale sessions on disconnect; `turnEnded` aborts that turn's pending commands; every close is bounded (Playwright close can hang on WS/SSE pages). Depends only on `playwright-core` — **zero Electron imports**.
- **Windows-ready executable discovery** (`adapters/src/browser/executable.ts`): explicit flag → Playwright pinned path → fallbacks scanning `PROGRAMFILES`/`PROGRAMFILES(X86)`/`LOCALAPPDATA` for chrome/chromium/msedge. Resolves the *user's* browser — no browser redistribution, no bundled-browser notices.
- **Tab ownership model.** The agent controls **only tabs it opened**. `listUserTabs` returns metadata only; taking a user tab requires explicit `claimTab` (desktop impl: `packages/desktop/src/main/browserView/browserGuestManager.ts`). Tabs persist for the process lifetime; `finalizeTabs({keep})` only *marks* tabs `handoff|deliverable` (deliverable releases the tab back to the user) and never closes anything (`docs/tab-cleanup-iab-internal.md`). Recovery protocol: each logical batch starts with a tab-list cell whose output is fully shown to the model, then a second call binds a verified id/url/title (`skills/control-browser/SKILL.md`).
- **Snapshot approach** (`adapters/src/browser/snapshot.ts`): in-page collection of actionable elements (≤200 refs `e1..eN` with role/name/text/value/rect/selector/xpath) + a ≤300-node DOM outline. The ref→ElementHandle map is kept **adapter-side in a `WeakMap`**, deliberately not on page `globalThis`, so page scripts can't swap it; stale refs fail closed with "take a fresh snapshot". Second layer: Playwright's own ARIA snapshot (`playwright-command.ts`).
- **Prompt-injection boundary.** Policy: page content is declared UNTRUSTED, used only to locate elements, never executed as instructions (`browser-use-plugin/docs/safety.md`, `skills/control-browser/SKILL.md`). Structural: page text reaches the model only as tool-result payloads; all control flow lives in skill/doc bundles. Blast-radius: navigation allowlist (http/https/about:blank — `file:`, `data:`, `javascript:` blocked), `evaluate` capped at 3 s and flagged last-resort. **Honest gap:** there is no string-level delimiter around page text in the tool channel — the boundary is prompt-policy + tool-channel structure, not a hard firewall.

### Portability split

| Class | Contents |
|---|---|
| **Portable, high value** | Port contract (545 lines, pure types); managed headless CDP adapter (≈1,550 lines, near 1:1 port to Python `playwright` async); snapshot scheme with anti-tamper ref handling; the two skills (`control-browser` 178 lines, `web-gui-tester` 157 lines — tooling-agnostic by construction, adaptable with s/`mcp__node_repl__js`/YAAH's browser tool/); capability-gated progressive-disclosure docs-manifest pattern. |
| **Adopt semantics, not code** | Tab ownership/persistence (≈50 lines of state over a CDP adapter); browser-client SDK's *rules* (fresh-kernel bootstrap, tab recovery, budgets) without its class hierarchy. |
| **Electron-coupled — skip** | In-app browser broker: `packages/desktop/src/main/browserView/browserGuestManager.ts` (**4,639 lines**) + ~14 siblings; UI: `packages/ui/src/browser-use/` (7 components, `<webview>`-hardwired). The headless adapter delivers full agent control without any of it. |
| **Skip — security surface** | Chrome profile import: Windows App-Bound cookie decryption via a signed elevated helper that creates and deletes a temporary system service (`packages/desktop/src/main/browserDataManager.ts`, `windowsChromeAppBoundKey.ts` 480 lines, native C# helper `packages/desktop/native/windows-browser-import-helper/Program.cs`). Massive attack surface; revisit only if login-state browsing becomes a requirement. |

**Premise correction (issue said profile import is "not supported on Windows").** That sentence exists **only in ZCode's hosted docs** (zcode.z.ai/en/docs/browser-use: "Importing browser data isn't supported on Windows yet — it's on the way"). The in-repo Windows implementation is actually *complete*: win32 profile discovery incl. policy dirs (`chromeProfileDiscovery.ts`), App-Bound decryption path (`chromeCookieManager.ts`), PowerShell-spawned signed helper (`windowsChromeAppBoundKey.ts`), Windows-specific elevation-flow UI strings. The doc statement is a product/shipping status, not an architectural Windows limitation. Recommendation stands for YAAH — treat one-time cookie/LocalStorage snapshot import as *feasible-on-Windows but high-risk* — but the cited reason should be the security surface, not "Windows doesn't support it."

---

## 3. Other borrow candidates (survey)

| # | Subsystem | What it is (evidence) | Value / effort |
|---|---|---|---|
| 1 | **Goal mode** | Persistent `SessionGoal` (objective/status/token/time budgets) drives automatic continuation turns, gated by a **separate verifier model call** before continuing; continuation prompts wrap the objective as untrusted data (`<untrusted_objective>`) and carry a battle-tested anti-premature-completion checklist; guard rails defer while background tasks run and respect a user Stop (`core/src/runtime/methods/target.ts`, `target-completion-verification.ts`, `contracts/src/tools/target.ts`) | Very high value / medium effort — loop-until-verified-done for scheduled agents; prompts + state machine directly portable |
| 2 | **Automations (cron)** | Model-facing `CronCreate` with meticulous schema refinements (cron vs relative delay vs interval carriers; prevents the model converting "in 8 minutes" into a wall-clock date); recursion guards (no CronCreate from an automation-dispatched turn); SQLite dispatch state machine with claim/heartbeat/retry/stale-reclaim (`contracts/src/tools/automation.ts`, `bootstrap/src/zcode-protocol/automation-port.ts`, `packages/services/src/session/automationRepo.ts`) | High value / medium effort — directly relevant to YAAH's scheduled agents |
| 3 | **Lifecycle hooks + trust digests** | Seven hook events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`, `Stop`) with input-rewrite and permission participation (`adapters/src/config/schema.ts`); workspace-supplied hook declarations are approved by **content digest** (`hooks trust grant --hook-digest <sha256>`, `cli/src/hooks-trust-command.ts`) — trust breaks automatically when declarations change | High value / low effort — event taxonomy copies into the Python loop; sha256 allowlist is a prompt-injection defense for repo-supplied hooks/skills |
| 4 | **Skills loader hardening** | Frontmatter key allowlist, 100 KB load cap, per-path disable, dedup by path not name, realpath canonicalization, diagnostics on every parse failure (`adapters/src/skills/index.ts`) | High value / low effort — exactly the edge cases YAAH's skill loader will hit |
| 5 | **Browser broker over named pipe** | Token-authed (`timingSafeEqual`) localhost IPC bridging sandboxed child → host browser, Windows named pipe on win32 (`bootstrap/src/app/node-repl-browser-broker.ts`); side-effect classification table for permission decisions (`adapters/src/browser/request.ts`); installed-browser resolution list | Medium value / medium effort; executable-resolution list is copy-paste |
| 6 | **Off-peak tasks** | "Run when idle / provider cheap" scheduler bound to a resumable parent session; anti-recursion; provider 429 queueing for idle-plan budgets (`bootstrap/src/zcode-protocol/offpeak-port.ts`, `adapters/src/model/offpeak-retry.ts`) | Medium value / medium effort — complements cron for BYO-key rate limits |
| 7 | **Subagent mailbox + run logs** | Filesystem mailbox: `unread/`→`read/` rename-based atomic drain with session-id path-escape validation (`adapters/src/mailbox/index.ts`); JSONL run store (append-only events + snapshot + `report.md`, `adapters/src/workflow/index.ts`) | Good value / low effort (~60-line idea) |
| 8 | **Context builder with budgets** | Every prompt section carries `chars`/`tokens`/`injectionTarget`/`cacheHint` (`core/src/context/builder.ts`) | Low effort / good value — model for YAAH's prompt manifests |
| 9 | **Typed memory convention** | One-fact-per-file markdown with typed frontmatter (`user/feedback/project/reference`), `[[name]]` wiki-links (dangling = "worth writing later"), one-line-per-memory index loaded each session (`core/src/context/sections/memory.ts`) | YAAH's memory already matches closely; the `[[name]]` convention and per-section token accounting are the borrowable bits |
| 10 | **Claude-plugin manifest compatibility** | Skill/plugin discovery accepts `.claude-plugin/plugin.json`, `.codex-plugin/`, `.cursor-plugin/` siblings (`adapters/src/skills/index.ts`) | Free ecosystem win / low effort |
| 11 | **Fails-closed placeholder + stable contract** | Environment-dependent capabilities ship as stable contract + explicit fails-closed stub (`packages/zcode-cua/index.js`) so prompts/tools stay stable when the capability is absent | Low effort — pattern worth copying for any YAAH capability gate |
| 12 | **Honest risk-disclosure NOTICE** | `NOTICE.md` is a per-capability risk table (what each subsystem can do, where data goes, what it does *not* guarantee); `scripts/licenses.mjs notices` generates `THIRD-PARTY-NOTICES.md` from the dependency graph with inventory + hashes (`third-party/inventory.json`) | Trivial effort — a template for YAAH's own notices/compliance pipeline |
| 13 | **Remote workspaces (SSH/WSL/Docker)** | First-class `remote:ssh\|wsl\|docker:<host>:<path>` workspace identity; WSL host pooling with idle TTL; SFTP asset upload (`windowRemoteConnectionRegistry.ts`, `v4-bridge.ts`, session-store migrations) | Identity model cheap to adopt; full connector layer high effort / low value for a Windows-first harness |

---

## 4. Licensing

**ZCode's own license:** Apache-2.0 (root `package.json`; full text in `LICENSE`). `apps/zcode-cli/packages/browser-use-plugin` and `packages/zcode-cua` also declare Apache-2.0; most other workspace packages declare no `license` field and fall under the root declaration. **No copyleft infection**: Apache-2.0 §4's final paragraph explicitly permits YAAH to keep its own license for the combined work.

**Attribution to carry into YAAH's third-party notices when adapting code** (exact text from `LICENSE` lines 190–202):

```
Copyright 2026 Z.AI Co., Ltd

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

**Apache-2.0 §4 obligations for adapted code:** (a) include a copy of the Apache-2.0 license with distributions; (b) carry prominent notices that files were modified; (c) retain all copyright/patent/attribution notices from adapted source files; (d) because the Work includes a NOTICE file, include a readable copy of its attribution content for the portions actually used (practically: credit *"ZCode — Copyright 2026 Z.AI Co., Ltd, Apache-2.0"*). ZCode's `NOTICE.md` is substantively a Chinese-language *risk-disclosure* document rather than attribution content; the §4(d) obligation attaches to whatever attribution notices it contains.

**Third-party components in the subsystems of interest:** the definitive inventory is `THIRD-PARTY-NOTICES.md` (50,240 lines, machine-generated by `scripts/licenses.mjs notices`, with versions/hashes in `third-party/inventory.json`). Sampling shows the npm section is entirely permissive — MIT, Apache-2.0, ISC, BSD-3-Clause (e.g. `@modelcontextprotocol/sdk` — MIT); **no GPL/LGPL/AGPL/MPL/EPL entries found** in the sampled range or in any workspace `package.json`. Caveat: not all 50k lines were read — a full sweep of `THIRD-PARTY-NOTICES.md` + `third-party/inventory.json` is the definitive pre-adoption check. `playwright-core` (Apache-2.0 upstream, with bundled-browser notices) is the key dependency for the browser path — note ZCode resolves the **user's installed browser** rather than shipping one, which avoids redistributing browser binaries and their notices entirely.

---

## 5. Recommendation table

| Subsystem | Verdict | Rationale |
|---|---|---|
| **CUA: operation-event protocol, permission schema/normalizer, display contract, stale-raster guard** | **Adopt (reimplement, ~600 lines of logic)** | Pure data transforms; instant "what is the agent doing" streaming and structured tool-result cards for YAAH's web UI |
| **CUA: broker/bridge transport pattern** (named pipe, token auth, one-shot, trusted `_meta`) | **Adapt** | The right seam between an isolated agent context and privileged desktop access; ~150 lines in Python asyncio |
| **CUA: subagent denial (policy + runtime)** | **Design-only** | YAAH already excludes CU from sub-agents; the transferable idea is defense-in-depth (deny at both layers) |
| **CUA: TCC onboarding, PiP, React renderers, Windows helper lifecycle** | **Skip** | Electron/macOS-coupled (~4,500 lines); YAAH's UIA driver can live in-process |
| **CUA: the executor itself** | **N/A** | Never open-sourced — fails-closed stub only; YAAH builds its own regardless |
| **Browser: port contract + managed CDP adapter + snapshot scheme** | **Adopt** (Python port of adapter ≈ near 1:1) | Zero Electron, Windows-correct executable discovery, battle-tested cancellation/generation/tab semantics |
| **Browser: control-browser / web-gui-tester skills + docs-manifest pattern** | **Adopt** (light rewrite) | Tooling-agnostic by construction; highest-value prompt assets in the repo |
| **Browser: tab ownership semantics** (agent-opened only, explicit claim, finalize-as-marks) | **Adopt** (≈50 lines of state) | Clean prompt-injection-adjacent safety model |
| **Browser: Electron IAB panel + UI components** | **Skip** | 4,600-line broker + `<webview>`-hardwired UI; headless adapter suffices |
| **Browser: Chrome profile import** | **Skip** (revisit if login-state browsing becomes a requirement) | App-Bound decryption via elevated temp-service helper — large security surface; feasible on Windows, but not worth the risk now |
| **Goal mode** | **Adopt (design + prompts)** | Best transplant in the repo for scheduled agents; verifier-gated continuation with untrusted-objective wrapping |
| **Automations schema refinements + dispatch state machine** | **Adapt** | Directly improves YAAH's scheduled agents |
| **Hooks taxonomy + digest-pinned trust** | **Adopt** | Cheap prompt-injection defense; well-chosen event set |
| **Skills loader hardening details** | **Adopt** | Edge cases YAAH's loader will hit, already solved |
| Everything else (survey §3) | Case-by-case | Individually noted value/effort above |

**Bottom line.** ZCode is worth mining, but not as a code drop for computer-use: the executor isn't there. The real haul is (1) a small, clean **CUA contract layer** worth reimplementing (~600 lines) plus a proven named-pipe broker seam; (2) a **fully present, Electron-free browser stack** — port contract, Playwright adapter, snapshot scheme, and two adaptable skills — that a Python port could reproduce nearly 1:1; and (3) a handful of harness ideas (goal mode above all) with prompt engineering already battle-tested. All Apache-2.0; obligations are the standard notice/license/attributions set quoted above.
