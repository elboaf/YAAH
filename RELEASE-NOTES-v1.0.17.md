# YAAH v1.0.17 — Release Notes

**Release:** v1.0.17 (stable — built from the rc.7 tree)
**Development window:** v1.0.16 (Sep 30) → Oct 3, 2026 · 49 changes · 47 PRs + 2 direct commits
**Full changelog:** https://github.com/elboaf/YAAH/compare/v1.0.16...v1.0.17

---

## Highlights

- **Read-aloud, reworked end to end** — narration can run on a remote TTS engine with voice discovery, a deterministic speech normalizer cleans up what gets spoken, per-emission narration can't be lost on UI remounts, and the say channel is fully toggleable ([#209](https://github.com/elboaf/YAAH/pull/209), [#210](https://github.com/elboaf/YAAH/pull/210), [#212](https://github.com/elboaf/YAAH/pull/212), [#227](https://github.com/elboaf/YAAH/pull/227), [#232](https://github.com/elboaf/YAAH/pull/232), [#234](https://github.com/elboaf/YAAH/pull/234), [#263](https://github.com/elboaf/YAAH/pull/263), [#264](https://github.com/elboaf/YAAH/pull/264)).
- **Interface scale** — a live 100–200% slider replaces the old preset buttons ([#215](https://github.com/elboaf/YAAH/pull/215)).
- **Prompt-surface quality push** — injection caps are consistent across mechanisms, duplicated guidance was consolidated into single homes, and auxiliary prompts now state the contract their consumers actually enforce ([#244](https://github.com/elboaf/YAAH/pull/244), [#245](https://github.com/elboaf/YAAH/pull/245), [#250](https://github.com/elboaf/YAAH/pull/250), [#251](https://github.com/elboaf/YAAH/pull/251)).
- **Scheduler controls** — one-shot run button, play/stop semantics, and scheduled prompts collapse into an expandable chip ([#235](https://github.com/elboaf/YAAH/pull/235), [#236](https://github.com/elboaf/YAAH/pull/236)).
- **Chat QoL** — right-click any selection to Search on Google, workspace file paths linkify, and chat titles are distilled from the user's actual request instead of echoing quoted or system text ([#261](https://github.com/elboaf/YAAH/pull/261), [#258](https://github.com/elboaf/YAAH/commit/40ce875), [#262](https://github.com/elboaf/YAAH/pull/262)).
- **Reliability** — fixed a backend crash when configuring an MCP server, killed the false "Agent run failed" toast on completed runs, and the recovery banner now waits for a manual Restart instead of auto-killing the backend ([#257](https://github.com/elboaf/YAAH/pull/257), [#265](https://github.com/elboaf/YAAH/pull/265), [#267](https://github.com/elboaf/YAAH/pull/267)).

---

## Read-aloud & voice

- Remote TTS engine for read-aloud narration — [PR #209](https://github.com/elboaf/YAAH/pull/209) (#205)
- Speech-friendly narration: deterministic normalizer + prompt rules — [PR #210](https://github.com/elboaf/YAAH/pull/210) (#206)
- Say-emission toggles: disable `<say>` generation + optional in-chat display — [PR #212](https://github.com/elboaf/YAAH/pull/212) (#207)
- Say briefings actually reach the frontend: wire fix + persistence + export + remote capture — [PR #227](https://github.com/elboaf/YAAH/pull/227)
- Remote TTS voice selector populates from the server's `/voices` — [PR #232](https://github.com/elboaf/YAAH/pull/232) (#231)
- Voice fix: speak every emission; fallback briefing no longer repeats the message — [PR #234](https://github.com/elboaf/YAAH/pull/234)
- Say-channel follow-ups from the #226 review — [PR #263](https://github.com/elboaf/YAAH/pull/263) (#230)
- Read-aloud: per-emission, remount-surviving narration latch — [PR #264](https://github.com/elboaf/YAAH/pull/264) (#237)

## Chat & desktop UI

- Interface scale: live 100–200% slider replaces preset buttons — [PR #215](https://github.com/elboaf/YAAH/pull/215) (#171)
- Chat: right-click selection → Search on Google — [PR #261](https://github.com/elboaf/YAAH/pull/261) (#201)
- Linkify workspace file paths in chat — [commit](https://github.com/elboaf/YAAH/commit/40ce875) (#258)
- Chat titles: distill the user's request from a quoted first message; reject conversational replies; provider `<system_*>` markup no longer becomes the title (sidebar warning triangle + `/handoff` tooltip) — [PR #262](https://github.com/elboaf/YAAH/pull/262) (#202), [PR #268](https://github.com/elboaf/YAAH/pull/268) (#255)
- Draft destination card: branch chip truncates, workspace select always wins — [PR #266](https://github.com/elboaf/YAAH/pull/266) (#253)

## Scheduled agents

- Scheduled agent runs: collapse the echoed prompt into an expandable chip — [PR #235](https://github.com/elboaf/YAAH/pull/235) (#198)
- Scheduled agents: one-shot run button + play/stop semantics — [PR #236](https://github.com/elboaf/YAAH/pull/236) (#199)

## Agent runtime & sub-agents

- Sub-agent schemas resolve on the workspace's own host; tool allowlist enforced at execution time — [PR #246](https://github.com/elboaf/YAAH/pull/246) (#188)
- Sub-agent prompt accuracy: exclusion clause derived from real sets; single final-message contract — [PR #247](https://github.com/elboaf/YAAH/pull/247) (#189)
- Sub-agent budget nudges: honest final-turn text + at most one convergence note — [PR #249](https://github.com/elboaf/YAAH/pull/249) (#190)
- Persistent-memory index injected into sub-agent prompts wherever memory tools resolve — [PR #223](https://github.com/elboaf/YAAH/pull/223) (#182)
- MCP injection hardening: server-name validation, exact routing, description clamp — [commit](https://github.com/elboaf/YAAH/commit/abad39e) (#194)
- One shared "Invoked skills" wrapper for both injection paths — [PR #260](https://github.com/elboaf/YAAH/pull/260) (#195)
- Skills: no not-found entries in the authoritative block; queued path emits `skill_not_found` — [PR #252](https://github.com/elboaf/YAAH/pull/252) (#193)

## Prompt surface & injection consistency

- Deduplicate sandbox guidance across win-local prompt sections — [PR #244](https://github.com/elboaf/YAAH/pull/244) (#186)
- Consolidate duplicated guidance between tool schemas and the sandbox section — [PR #245](https://github.com/elboaf/YAAH/pull/245) (#187)
- Auxiliary prompts state the contract their consumers enforce (summarizer clamp, injection label, title guidance) — [PR #250](https://github.com/elboaf/YAAH/pull/250) (#191)
- Injection-mechanism cap consistency: no duplicate memory heading, marked truncations, clamped skill descriptions — [PR #251](https://github.com/elboaf/YAAH/pull/251) (#192)
- Derive prose tool lists from the schema sets — [PR #222](https://github.com/elboaf/YAAH/pull/222) (#181)
- Tool-description and lazy-tier doc corrections — [PR #217](https://github.com/elboaf/YAAH/pull/217) (#197)
- Sandbox-only note states the actual risk-based gate contract — [PR #216](https://github.com/elboaf/YAAH/pull/216) (#177)
- Sandbox guidance only when sandbox tools exist; remote-offline path applies the plan-mode note — [PR #220](https://github.com/elboaf/YAAH/pull/220) (#179), [PR #218](https://github.com/elboaf/YAAH/pull/218) (#178)

## Memory

- Settings toggle: persistent memory opt-in, default OFF — [PR #213](https://github.com/elboaf/YAAH/pull/213) (#169)
- Memory charter: memory models the owner, not a log of actions — [PR #229](https://github.com/elboaf/YAAH/pull/229) (#228)

## Fixes & reliability

- fix(#256): restore the #128 MCP client lost in a merge — configuring a server crashed the backend — [PR #257](https://github.com/elboaf/YAAH/pull/257)
- A completed run never settles `error` — no more false "Agent run failed" toast — [PR #265](https://github.com/elboaf/YAAH/pull/265) (#243)
- Recovery banner: manual Restart backend instead of watchdog auto-kill, with downtime count-up — [PR #267](https://github.com/elboaf/YAAH/pull/267) (#254)
- bash/powershell timeout: return partial output instead of discarding it — [PR #221](https://github.com/elboaf/YAAH/pull/221) (#180)
- Enforce the 100 KB inline-attachment cap at the injection layer and the turn APIs — [PR #224](https://github.com/elboaf/YAAH/pull/224) (#183)

## Toolkit & infrastructure

- Re-land: `toolkit install` wrapper (state.json + INDEX.md) + six review findings — [PR #248](https://github.com/elboaf/YAAH/pull/248) (#118)
- Manifest harness: `-compaction` combos actually render the compaction summary; live watermark + harness self-check — [PR #225](https://github.com/elboaf/YAAH/pull/225) (#184)
- Prompt-surface inventory: sweep stale line refs + drift-guard test — [PR #219](https://github.com/elboaf/YAAH/pull/219) (#196), [PR #241](https://github.com/elboaf/YAAH/pull/241), [PR #239](https://github.com/elboaf/YAAH/pull/239)
- ZCode evaluation: CUA + built-in browser portability findings (research) — [PR #214](https://github.com/elboaf/YAAH/pull/214) (#170)
- test-build workflow made manual-only (no PR trigger) — [PR #240](https://github.com/elboaf/YAAH/pull/240)

---

## RC timeline

| Tag | Date (UTC) | Contents |
|---|---|---|
| v1.0.17-rc.0 | Oct 1, 00:22 | Line opener (cut minutes after v1.0.16; no PRs yet) |
| v1.0.17-rc.1 | Oct 1, 04:36 | Remote TTS (#209), speech normalizer (#210), say toggles (#212) |
| v1.0.17-rc.2 | Oct 1, 14:28 | Say-channel wire fix (#227) |
| v1.0.17-rc.3 | Oct 1, 21:51 | Scheduler controls (#235 #236), voice discovery + fix (#232 #234), memory charter (#229), sub-agent memory index (#223), inline cap (#224), bash timeout output (#221), sandbox/plan docs (#216 #218 #220), tool docs + prose lists (#217 #222), ZCode eval (#214) |
| v1.0.17-rc.4 | Oct 2, 03:21 | Prompt-surface inventory sweep + drift guard (#219 #241), test-build manual-only (#240), version bump (#242) |
| v1.0.17-rc.5 | Oct 2, 13:46 | Toolkit install wrapper re-land (#248), sub-agent host resolution + prompt accuracy (#246 #247), compaction manifest harness (#225) |
| v1.0.17-rc.6 | Oct 2, 15:33 | MCP client restore — server-config crash fix (#257) |
| v1.0.17-rc.7 | Oct 3, 05:33 | Interface scale slider (#215), prompt dedup + cap consistency (#244 #245 #250 #251), budget nudges (#249), skills wrapper + skill_not_found (#260 #252), chat QoL wave (#261 #262 #268), say/narration follow-ups (#263 #264), false-toast + recovery + chip fixes (#265 #267 #266) |

---

*Installers for every RC are attached to each release as `yaah-desktop-setup.exe/.deb` and `yaah-server-setup.exe/.deb` (unsigned — expect an OS warning on first run).*
