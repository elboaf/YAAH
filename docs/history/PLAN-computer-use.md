# Plan: Computer use — run an app and control / test it

Status: decided (grilled 2026-01-24). Every open question below was answered by the user; consequences are recorded where an answer overrode the recommendation.

## Idea

Give YAAH computer use: the ability to run an application and control / test it with real mouse movement, keyboard input, and screenshots-for-vision. The agent launches (or attaches to) an app on the user's Windows desktop, drives its UI, and verifies results by looking at the screen. This is app-testing focus, not a general desktop agent: the tools exist to test software, and the system prompt says so.

## Decisions (user-confirmed)

| # | Question | Decision |
|---|----------|----------|
| Q1 | Scope of control | **App-testing focus.** Tools are general (the OS has no scoped clicks), but the prompt constrains the agent to apps under test. |
| Q2 | Platform scope | **Windows-only, hard line.** Tools registered only on Windows; no degradation path on macOS/Linux (same pattern as `powershell`). |
| Q3/Q6 | Mechanism | **Python libs in the backend: pynput** (input) **+ mss** (screenshots). No Rust/Tauri changes. |
| Q4 | Consent gate | **None on Windows.** Tools are always registered on Windows. The panic key + user-activity pause carry the trust load. |
| Q5 | Launch model | **Shell launches, open window list.** No `launch_app` tool — apps are started via the existing shell tools (`Start-Process` etc.), and `list_windows` shows ALL top-level windows with PID + process name so the agent can find its target. |
| Q7 | v1 tool surface | **Core set, full-screen shots.** screenshot, list_windows, focus_window, mouse_move, mouse_click, mouse_scroll, type_text, press_key, wait. No drag; no window-targeted (PrintWindow) capture in v1. |
| Q8 | Safety mechanisms | **Panic hotkey + user-activity pause.** Hotkey force-cancels turns; injected actions refuse while real user input is recent. |
| Q9 | Panic hotkey | **Configurable** in config.json (default `Ctrl+Alt+Y`), validated at startup; a pynput `GlobalHotKeys` listener in the backend fires `cancel_agent` for every active conversation. |
| Q10 | Pause semantics | **2s cooldown + paused result.** Injected actions return `{"error": "user-activity pause …", "paused": true}` while real input was seen in the last ~2s; the agent re-screenshots and retries when idle. Nothing auto-cancels. |
| Q11 | Multi-monitor | **Primary monitor default + `monitor` param.** No stitched ultrawide captures (bad for vision tokens). |
| Q12 | Remote sessions | **Client-local, never forwarded.** Computer-use tools are excluded from `REMOTE_TOOLS`; a remote host's desktop is never driven over the LAN protocol. |
| Q13 | Privacy of screenshots | **Accepted with prompt-level discipline.** The prompt says: screenshot only when the task requires seeing the screen, never to inspect the user's other work; docs note screenshots go to the configured model provider. No capture gate in v1. |
| Q14 | Testing | **Fakes in pytest + one opt-in live test.** Tool layer tested against fake pynput/mss bindings (never real input in CI); one `live`-marked integration test drives Notepad, run locally via env flag. |
| Q15 | Dependency shipping | **Core deps.** pynput + mss in `requirements.txt` and the PyInstaller bundle unconditionally (Windows hard line makes lazy-import degradation pointless). |
| Q16 | Act-then-see | **`observe` param, default off.** Input tools return compact text; `observe: true` attaches a post-action screenshot in the same tool result (saves a round-trip when chaining). Screenshots stay purposeful per Q13. |
| Q17 | Guidance | **Bundled playbook skill** (`computer-use`) in `backend/bundled_skills/` — visual anchoring, wait-for-load, retry discipline. System prompt carries only a short section; the skill carries the depth. |
| Q18 | Deliverable | **This plan.** `/to-spec` and `/to-tickets` come later, when build starts. |

### Accepted hazards (recorded, not bugs)

- **No gate (Q4) + full-screen shots (Q7/Q13):** any conversation's model can capture the entire screen — including unrelated windows — and send it to whatever provider is configured. User explicitly accepts this; the mitigation is prompt discipline plus the transparency principle (every screenshot shows in the trace).
- **Injected input is visible to our own listener:** pynput injection goes through SendInput, so the activity detector would see the agent's own actions as "user input" and pause forever. Resolution is mandatory injected-event filtering (see Architecture) — the one genuinely tricky bit in this feature.
- **Pause starvation (Q10):** a user typing continuously during a run keeps the agent paused. Accepted; the agent reports it and waits, and the user can stop the turn.
- **Hotkey collision (Q9):** a user-chosen combo may conflict with an app's shortcut. Accepted; combo is configurable and validated, default is deliberately obscure.
- **Q13 note, restated:** screenshots flow to the model provider — the one exception local-first already makes. No first-capture confirmation UI in v1.

## Tool surface (v1)

All schemas + executors live in a new `backend/agent/computer.py`, registered into `TOOLS_SCHEMA` / `EXECUTORS` on Windows only (mirrors the `POWERSHELL_SCHEMA` pattern: schemas appended in `get_schemas()` when `os.name == "nt"`, executors registered at import). Non-Windows: the tools don't exist for the model, and a direct call falls through to the existing "Unknown tool" error.

| Tool | Signature | Notes |
|------|-----------|-------|
| `screenshot` | `monitor: int = 1` | mss full-screen grab of that monitor → `imagedata.save_bytes(raw, "png", "screenshots")` → returns `{"image": rel, "monitor": n, "size": [w, h]}`. The existing loop converts `image` into a vision part, trace entry, and DB row — zero loop changes. |
| `list_windows` | — | EnumWindows via ctypes: hwnd, title, pid, process name, monitor. The agent's way to find its app after a shell launch. |
| `focus_window` | `hwnd: int` | SetForegroundWindow; `observe` allowed. |
| `mouse_move` | `x: int, y: int` | Absolute desktop coords. |
| `mouse_click` | `x: int, y: int, button: "left"\|"right" = "left", double: bool = false` | The workhorse. |
| `mouse_scroll` | `x: int, y: int, amount: int` | Scroll at position (positive = up). |
| `type_text` | `text: string` | pynput unicode typing (VK_PACKET). |
| `press_key` | `key: string` | Chord syntax `"ctrl+s"`, `"alt+f4"`. |
| `wait` | `seconds: float` | Sleep for UI paint / load; cheaper than a retry storm. |

- **`observe: bool = false`** on `focus_window`, `mouse_move`, `mouse_click`, `mouse_scroll`, `type_text`, `press_key`. When true, the tool takes a screenshot after acting and returns it in the same result (only the input tools take this; it's how the agent chains act-verify without extra round-trips).
- **No `launch_app` / `kill_app`** (Q5): the agent uses `powershell`/`bash` (`Start-Process`, `taskkill /PID`) exactly as it does today. Launch lifecycle is the shell's business; the window list is the agent's map.
- **No drag, no PrintWindow capture, no `launch_app`** in v1 — all three are clean fast-follows with existing seams.

## Architecture

### New module: `backend/agent/computer.py`

Schemas, executors, and the three runtime pieces below. Thin internal seams so tests can fake them:

- `_input` — wraps pynput mouse/keyboard controllers (only the app imports pynput).
- `_capture` — wraps mss.
- `_activity` — the listener/detector described below.

### User-activity detector + injected-event filter (the tricky bit)

- One pair of pynput listeners (mouse + keyboard) started at backend startup on Windows. Real input updates `last_user_input = time.monotonic()`.
- **Injected-event filtering:** SendInput events DO arrive at low-level hooks. The detector must not count the agent's own actions, or every automation action pauses the next one forever. Use a small ctypes low-level hook (`SetWindowsHookEx WH_MOUSE_LL` / `WH_KEYBOARD_LL`) that reads `LLMHF_INJECTED` / `LLKHF_INJECTED` and ignores injected events — deterministic, no timing races. (Fallback if the hook proves fragile: suppression windows stamped around each injection, accepted as race-prone.)
- Pause check in every input executor: `if monotonic() - last_user_input < 2.0: return {"error": "user-activity pause: real input Ns ago — screenshot to re-verify and retry when idle", "paused": True}`.

### Panic hotkey

- pynput `GlobalHotKeys` listener at startup; combo from config.json `computer_use.panic_hotkey` (pynput format `"<ctrl>+<alt>+<y>"`), validated with a real GlobalHotKeys construction; invalid → log + default.
- Fires a new "cancel all active turns" helper (iterate active conversations → existing `loop.cancel_agent`). The listener lives in the backend process, so it works even when YAAH's window is buried behind the app under test.

### Screenshots

- mss monitor 1 by default; `monitor: n` for others (1-based, matching mss's `monitors` indexing after its 0=virtual-all entry is skipped).
- Full resolution, PNG, stored + returned via the existing `imagedata` path. Optional max-width downscale is a fast-follow if 4K captures prove too heavy in practice.

### Integration points (all tiny)

- `tools.py`: import + register from `computer.py` on Windows (schemas in `get_schemas()`, executors in `EXECUTORS`).
- `remote.py`: **untouched** — computer tools are client-local by omission from `REMOTE_TOOLS`; no protocol bump.
- `loop.py`: no changes (the `{"image": rel}` contract already does everything); only the prompt section below is added.
- `main.py` (startup): start activity detector + hotkey listener on Windows.

### Prompt + skill

- Short system-prompt section (Windows only, in `_default_system_prompt`): what the tools are for (testing apps), window-scope discipline (don't interfere with windows outside the task), screenshot discipline (Q13), the pause-result contract, and that `Ctrl+Alt+Y` (or configured combo) stops everything.
- Bundled skill `backend/bundled_skills/computer-use/SKILL.md`: the playbook — find the window from `list_windows`, focus before typing, wait-for-load patterns, visual anchoring for click targets, retry discipline (screenshot → adjust → retry, max N), `observe` vs explicit screenshots, reading app logs from the shell that launched it, driving modal dialogs in the app under test.

## Testing

- `backend/tests/test_computer.py` — all against fakes (monkeypatched `_input` / `_capture` / `_activity` / window enums):
  - pause semantics: action refused within cooldown, allowed after; paused result shape.
  - injected-event filter: injected events never refresh `last_user_input` (fixture replays both event kinds at the hook layer).
  - hotkey config: valid combo, invalid combo → default, missing config.
  - registration: schemas/executors present on Windows, absent elsewhere.
  - `observe` wiring: post-action capture attached, default off.
  - `list_windows` parsing from fake EnumWindows output.
- One live integration test, `@pytest.mark.live`, gated on `YAAH_LIVE_COMPUTER=1`, never in CI: launches Notepad, types text, saves to a temp file via `ctrl+s` flow, screenshots and asserts via the existing shell tools. Doubles as the dogfooding seed: YAAH testing an app.
- Real QA is dogfooding: point YAAH at a small GUI app and let it test it.

## Rollout notes

1. **M1 — see:** `screenshot` + `list_windows` + `focus_window`. Read-only; immediately useful ("look at this screen and tell me…") and it exercises the whole image pipeline.
2. **M2 — act:** input tools + activity detector + injected filter + panic hotkey + pause contract.
3. **M3 — guide:** prompt section, `observe` param, bundled `computer-use` skill, docs (privacy note: screenshots go to the configured provider).
4. **M4 — prove:** live Notepad test + dogfood session testing a real app (candidate: YAAH itself).

Fast-follows, deliberately out of v1: `mouse_drag`, window-targeted capture (PrintWindow, works when occluded), optional max-width downscale, element-location helpers, remote-host forwarding (protocol bump + host consent), macOS/Linux.
