---
name: sandbox-testing
description: Rules and playbook for keeping test/verification work INSIDE the Windows Sandbox VM — run the app, its dependencies and its GUI automation there, never on the host. Use whenever a task involves sandbox_test/sandbox_run, testing or reproducing bugs in a disposable VM, or GUI-driving a sandboxed app.
---

# Sandbox testing: everything happens in the VM

The Windows Sandbox (boot with `sandbox_test`, drive with `sandbox_run`)
is the default place to run the workspace's app, tests and builds. Two
hard rules define "inside":

1. **Dependencies run in the VM, never as host equivalents.** If the app
   under test needs a browser, a runtime, or a portable tool, install or
   download it inside the sandbox (into the toolkit so it persists) and
   run it there. Launching the host's browser/python/server to exercise
   a sandboxed app is a failure — it pollutes the user's machine, which
   is exactly what the sandbox exists to prevent.
2. **GUI input happens in-VM.** The host `mouse_click`/`type_text`/
   `press_key` tools move the USER'S real mouse and keyboard — never use
   them on the sandbox's windows. The VM has its own input session: use
   AutoHotkey v2 via `sandbox_run` (see the sandbox section of the
   system prompt for setup and verified ControlClick/ControlSend
   patterns). `screenshot`/`read_ui_tree` on the host are fine — they
   observe without touching input.

## Playbook

- Boot: `sandbox_test` (first boot is slow; later boots reuse). The
  workspace appears at `C:\Users\WDAGUtilityAccount\Desktop\ws`; the
  persistent toolkit is at `...\Desktop\toolkit` and on PATH.
- Missing tools: install into the toolkit (zip/portable preferred,
  silent flags for installers) — it persists across all future
  sandboxes, so you only pay once.
- Verify like you would on the host: run tests via `sandbox_run`, batch
  commands (each round-trip costs ~1-3s), size `timeout_seconds` to the
  work, and check output before deciding the next step.
- Done: `sandbox_stop`. The VM is an 8 GB window on the user's desktop.
