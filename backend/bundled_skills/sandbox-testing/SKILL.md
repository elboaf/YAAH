---
name: sandbox-testing
description: How to decide when Windows Sandbox is warranted and how to use it safely. Use when a test opens a network port/server, needs visual GUI inspection, could disrupt the host user, or when operating sandbox_test/sandbox_run.
---

# Sandbox testing: isolate disruptive work

Choose the environment by side effects. Run automated tests and validation on the host by default—including full suites, builds, Python scripts, smoke tests, typechecks and lint—when they won't open a new window or reasonably interfere with or interrupt the host user. A test's size or the fact that it executes project code does not by itself require Sandbox.

Use Windows Sandbox (`sandbox_test`, then `sandbox_run`) when project execution opens/listens on a network port, especially when starting a server; when a GUI window must be opened to inspect visual appearance; or when a test could otherwise interfere with or interrupt the host user. When a test has a safe mock/fake mode that avoids these effects, that mode can generally run on the host.

## Safety rules inside the sandbox

1. **Dependencies run in the VM, never as host equivalents.** If the app under test needs a browser, runtime, or portable tool, install or download it inside the sandbox (into the toolkit so it persists) and run it there. Do not launch the host's browser/runtime/server to exercise a sandboxed app.
2. **GUI input happens in-VM.** Host `mouse_click`/`type_text`/`press_key` tools move the user's real mouse and keyboard. For a sandbox app, drive the GUI with the windows-mcp MCP server (auto-started at boot; see the system prompt's sandbox section). Host screenshots and `read_ui_tree` may observe the Sandbox window without touching its input.

## Playbook

- Boot with `sandbox_test` (first boot is slow; later boots reuse). The workspace appears at `C:\Users\WDAGUtilityAccount\Desktop\ws`; the persistent toolkit is at `...\Desktop\toolkit` and on PATH.
- Before downloading tools, read `toolkit\state.json`. Install missing tools into the toolkit (zip/portable preferred, silent flags for installers); it persists across sandboxes.
- Run the selected test commands via `sandbox_run`, batch commands where possible, and size `timeout_seconds` to the work.
- Dispose the VM with `sandbox_stop` when done. The VM is an 8 GB window on the user's desktop.
