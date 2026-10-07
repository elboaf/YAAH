# ADR 0013: Host computer use removed — GUI control only via sandbox + windows-mcp

Date: 2026-10-05
Status: Accepted
Driven by: #299 (cursor hitches during test runs), #339 (spec), maintainer decision

## Context

YAAH shipped a host-side computer-use stack: `screenshot`, `list_windows`,
`read_ui_tree`, `focus_window`, `mouse_move/click/drag/scroll`, `type_text`,
`press_key` and `wait` tools driving the user's real desktop, plus two
resident low-level input mechanisms for safety/awareness:

- `WH_MOUSE_LL` / `WH_KEYBOARD_LL` activity hooks (`SetWindowsHookExW`,
  threads `yaah-activity-*`) powering the user-activity pause;
- a pynput global keyboard listener implementing the panic hotkey
  (default ctrl+alt+y) for urgent turn cancellation.

Low-level hooks are serviced on the installing process's hook thread. When a
pytest run saturated the machine's cores, that thread was starved and the
**cursor hitched system-wide** (#299: pointer-only hitches, rest of the UI
smooth, tightly tracking test runs, only ever on the locally attached
console). YAAH was the only affected project because no other project ran a
hook-hosting process on the desktop while its tests executed. CI was never a
suspect (the suite already ran on the self-hosted Linux runners), and the
offload-to-another-host ideas (Linux runners for dev runs, a dedicated SSH
test box) were considered and rejected — the cost is the hooks, not the
tests' weight.

## Decision

**YAAH gets out of the business of touching the host desktop at all.**

- All host desktop tools are removed from the tool registry, system prompt,
  help docs, and frontend (Settings screenshot toggle gone with them).
- The activity hooks, user-activity pause, panic hotkey, and pynput/mss/
  uiautomation/comtypes/Pillow dependencies are removed outright. The
  maintainer explicitly chose gutting over reimplementations (no
  `RegisterHotKey` replacement for the panic hotkey, no `GetCursorPos` poll
  replacement for the activity pause): urgent stop is the chat UI's stop
  button.
- GUI inspection/automation survives **only** inside the Windows Sandbox VM
  via the windows-mcp MCP server (sandbox_test/run/status/stop unchanged) —
  the VM has its own input session, so host input is never touched.
- `computer_use` config keys from existing installs are ignored gracefully:
  `load_config` merges unknown keys through, and neither the backend nor the
  config API reads them anymore.
- The `live` pytest marker / `YAAH_LIVE_COMPUTER` gate died with the live
  input tests. `backend/tests/test_issue_339_no_input_hooks.py` is the
  tripwire: no `backend.agent.computer` module, no hook libraries in
  `sys.modules`, no host desktop tools in the registry, and no hook
  machinery anywhere in backend source (vendored windows-mcp inside the
  sandbox toolkit exempt — it runs in the VM).

## Consequences

- #299's primary suspect is structurally eliminated; no instrumented A/B
  repro is needed. If cursor hitches persist with zero LL hooks installed,
  the hypothesis was wrong and #299 reopens with new evidence.
- Agents can no longer see or drive the host desktop at all. Host GUI work
  happens by booting a sandbox VM and driving it through windows-mcp.
- The prompt-manifest matrix lost the shot/noshot axis (76 files, was 96
  win-local renders); `backend/prompt_manifests/` is regenerated in the
  same change, per ADR-0011's drift guard.
- The PyInstaller specs (git-ignored, local-only) shrink with the
  requirements; their hiddenimports lists no longer name pynput/mss/
  uiautomation/comtypes/Pillow.
