# Plan: Release v1.0.2

Status: scoped (2026-09-20). Scope picked by the user from the full open-issue triage; the one open design question (#6 control placement) was answered by the user. Triage covered all 20 open issues at the time of writing.

## Scope

**In (6 issues):** #34, #33, #29, #31, #32, #6
**Shipped early in v1.0.1 (dropped from this scope):** #25 — the sidebar run-status indicator landed in `b2a7e13` (PR #35) alongside the v1.0.1 bump.
**Housekeeping (5 issues closed 2026-09-20, no code):** #10, #12, #19, #20, #21 — all five had merged implementation commits; closed with comments pointing at them.

| # | Title | Evidence already shipped |
|---|-------|--------------------------|
| #10 | Multiple concurrent chats | `a2de26f` feat: multiple concurrent chats (#23) |
| #12 | Bundled Git installer + `install_git` | `f02e3de`, `backend/agent/gitenv.py` + `test_gitenv.py` |
| #19 | Nudge: in-sandbox tools, not host | `f02e3de`, nudge injection in `loop.py` |
| #20 | Nudge: don't use host mouse/keyboard | `f02e3de` |
| #21 | PTT toast on every settings save | `f02e3de`, guard comment in `components.tsx` |

**Out (deferred to v1.0.3 candidates):**

| # | Title | Why deferred |
|---|-------|--------------|
| #30 | Run visualizer (node tree slide-out) | Large feature; well-specced but a release of its own |
| #15 | Live sub-agent cards + turn-budget grace turn | Backend + frontend, two distinct parts |
| #7 | Queued messages + steer | Issue itself says "needs $grill-me" — design-heavy |
| #8 | Move chats between workspaces | Pairs naturally with #32 but is its own DB + UI change |
| #9 | Skill drawer | Underspecified one-liner; needs a design pass |
| #24 | Bundle GitHub CLI | License + build-sidecar work, same shape as #12 |
| #28 | Branch selector aligns from chat context | Real behavior change with dirty-worktree hazards; needs its own grilling |
| #27 | Sandbox 0x80072746 mid-session crash | Investigation item; schedule when it bites, not on a plan |

## Work items, in implementation order

### 1. #34 — README: what YAAH stands for
One line under the title: **YAAH = Yet Another Agent/Harness**. Trivial docs change.

### 2. #33 — grilling skill renders questions twice
Rewrite the round-format section of `backend/bundled_skills/grilling/SKILL.md` per the issue's suggested fix: drop the ❓ Q1 / ➡️ / `---` chat template entirely; state that each question reaches the user via one `ask_user` call (title + body in `question`, choices as `options`, recommended answer first) and that questions must **not** also be rendered as formatted text in chat. Keep rounds, frontier recomputation, and the facts-vs-decisions split unchanged. No code.

### 3. #29 — notification chimes (run finished / question pending)
- Two short distinct bundled chimes (WebAudio `AudioContext` or `<audio>`; no new dependency), moderate volume.
- **Run finished:** fires on every conversation's `streaming → idle` transition — mirror the existing `wasStreamingRef` TTS pattern (~L4166) per conversation buffer, with the same guards so history loads / conversation switches stay silent. Fires even when focused, for every conversation when several run at once (#10).
- **Question pending:** on `pendingQuestions[key]` appearance (store.ts), guarded per `call_id` (never on re-render / history load / conversation switch — same approach as `spokenQuestionRef` ~L4181), **only when Yaah is unfocused** (`document.hidden` or window blur).
- **Settings:** mute toggle, default ON, persisted via `updateConfig` like `voice.tts_enabled`.
- Chime plays before TTS speech when both are on; independent of the TTS toggle.
- Plan defaults for the issue's open questions (flag if you disagree): errored/cancelled runs play the **same** run-finished chime (the status change is the signal; a distinct error sound can come later); **no** throttling when several conversations finish within a second (rare; mute exists).
- Verify the Tauri webview has no autoplay-policy surprise with the window minimized/backgrounded.

### 4. #6 — model thought level (design decided by user)
**Decision:** global Settings dropdown, matching the existing temperature/max_tokens pattern.
- Settings gains a **Reasoning effort** select: **Default** (don't send the param) / Low / Medium / High.
- Config key `reasoning_effort` (default `""`), saved through the existing settings flow (`updateConfig`); `load_config()` passes it through like `temperature`.
- `model_client.chat()` adds `payload["reasoning_effort"]` only when set — the default never sends it, so providers that reject the param are unaffected until the user opts in.
- Accepted hazard: an OpenAI-compatible server that hard-rejects unknown params will 400 when a level is selected; the fix is setting the dropdown back to Default. Documented in the Settings hint text ("only affects reasoning-capable models").
- Test: payload-building unit test (param present iff set) alongside the existing model_client tests.

### 5. #32 — destination card on the new-chat screen
- Draft state (empty state / above the composer) shows **"This chat will be saved to: \<workspace\>"** — precise identity, full path as secondary line / hover.
- **Change…** affordance: picker over registered workspaces in the current scope (local registry while local; host's registry while remote, respecting `host::path` namespacing via `parseNsWorkspace`), plus an "Add workspace…" escape hatch reusing the existing flows (incl. Tauri `pick_workspace`).
- **Decision (issue's open question): draft-local destination, hybrid default.** The card mirrors the live active workspace (open conversation / expand group / add workspace all update it — acceptance criterion 3) until the user explicitly picks via Change…, which pins a draft-local destination that survives further active-workspace churn. Draft-local keeps the core invariant ("the open conversation's workspace IS the active workspace") intact: nothing flips globally pre-send, and after first send, opening the new conversation aligns the active workspace to its filed home as today.
- First send files the conversation under the chosen destination (`createConversation(..., workspace)` in `send()`, ~L5524, currently passes the globally active workspace — switch to the draft destination). Card disappears once saved.
- Changing destination mid-draft keeps typed text + staged attachments intact — only the filing target changes.
- Default (no-root) pseudo-workspace is a valid, shown destination.

### 6. #31 — hyperlinks open in the OS default browser
- `src-tauri/Cargo.toml`: add `tauri-plugin-opener`; grant `opener:allow-open-url` in `src-tauri/capabilities/default.json`.
- Frontend: intercept clicks on the `a` override in `src/markdown.tsx` and the image-attachment link (`src/components.tsx` ~L1071) → `openUrl(href)` + `preventDefault()`, gated on running under Tauri; plain `<a target="_blank">` fallback for vite dev / vitest.
- Keep the `safeHref` scheme allowlist (`https?:` / `mailto:`) as the gate on what reaches the OS; `javascript:` etc. stay inert.
- **Touches Rust → requires a cargo rebuild.** Do this last among the code items so the running v1.0.1 build is never blocked, and the v1.0.2 build happens once, after everything else lands.

## Release mechanics

- Version bump = `package.json` only (per `1507652` — it's the single version file a release bumps; `tauri.conf.json` references it).
- Sequence: land items 1–5 → item 6 (Rust) → `npm run tsc` + `vite build` + vitest + pytest → bump to 1.0.2 → tag `v1.0.2` → build.
- The v1.0.1 build currently running is untouched; v1.0.2 work starts on master after it completes.
- Housekeeping: close #10, #12, #19, #20, #21 with a comment each pointing at the merged commit.

## Accepted hazards

- **#6:** providers that reject `reasoning_effort` 400 on a non-Default setting until it's reset (documented in Settings hint).
- **#29:** chime-on-finish fires for background conversations too — by design (#10); users who find it chatty mute in Settings.
- **#32:** a pinned draft destination can disagree with the active workspace at send time (that's the feature); the sidebar grouping after send is the source of truth.
