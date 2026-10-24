# Implementation plan: live Windows Sandbox preview (issue #92)

## Goal

Give the user a small, always-on-top, view-only live preview of the Windows Sandbox client window while preserving the existing in-VM interaction path. Use the Windows DWM thumbnail API directly; do not add an external GUI/runtime dependency.

## Decisions

- The preview is host-desktop UI and is enabled only on interactive Windows runs; headless and non-Windows runs must remain unaffected.
- Phase 1 does **not** hide, minimize, move, or change the z-order of the real sandbox window. This avoids changing the user's desktop state while preview discovery and rendering are proven.
- Use a small native Win32 destination window and `DwmRegisterThumbnail` / `DwmUpdateThumbnailProperties` / `DwmUnregisterThumbnail`, hosted on a dedicated thread with its own message pump. Do not block FastAPI's event loop or add Tk/AHK/pywin32 dependencies.
- DWM rendering is for the user's glanceable view only. It does not provide pixels to the model or replace `vm-capture`/Windows.Graphics.Capture.
- DWM/window failures are non-fatal to sandbox startup and command execution.

## Phase 1 — live preview with safe baseline cleanup (start now)

1. Discover the visible `WindowsSandboxClient.exe` top-level window after sandbox readiness, including already-running and reattached sessions.
2. Open a compact, always-on-top, non-activating/click-through native preview and register a DWM thumbnail for that source window. Keep the real window untouched.
3. Own the preview on a dedicated thread; unregister the thumbnail and destroy the preview window on explicit `sandbox_stop`, source-window disappearance, or preview initialization failure.
4. Make start/stop idempotent and best-effort. An unsupported/headless context or DWM failure logs/skips the preview without failing a sandbox operation.
5. Add fakeable tests for lifecycle call sites and manager idempotency/cleanup behavior, plus an interactive Windows smoke test when available.

**Acceptance:** successful new start, reuse, and reattach attempt to show one preview; explicit stop removes it; no source or DWM failure leaves the sandbox operational; non-Windows tests remain green; real sandbox window is not repositioned/minimized.

## Phase 2 — lifecycle hardening

1. Reconcile preview state with sandbox state across boot timeout/retry, command-detected VM crash, transparent reboot, backend graceful shutdown, and app restart/reattachment.
2. Handle source HWND replacement/title changes without duplicate previews; close promptly on VM exit and retry when an adopted/running VM's client window appears late.
3. Harden shutdown against thread races, DWM/API exceptions, and partial initialization; ensure logging is useful but non-fatal.
4. Add tests for each lifecycle transition and a Windows integration probe for DWM registration, HWND replacement, and cleanup.

**Acceptance:** no stale preview after stop/crash/shutdown; one preview after recovery or reattachment; all failures degrade to the existing sandbox behavior.

## Phase 3 — optional backgrounding of the real sandbox window

1. Only after phases 1–2 are stable, consider sending the source behind other windows with `SetWindowPos(HWND_BOTTOM)` while keeping it unminimized. Do **not** move it off-screen or use `WinHide`.
2. Preserve the original placement and relevant z-order/foreground state, and restore them on preview close/stop. Do not background a window the user has deliberately activated since preview creation.
3. Gate this behavior behind an explicit opt-in setting or user action; default remains preview-only.
4. Test foreground/z-order restoration and failure cases on real Windows Sandbox. If DWM becomes blank/stale or safe restoration cannot be guaranteed, do not ship backgrounding.

**Acceptance:** opt-in only, no minimize/off-screen behavior, reliable restoration, and no impact on normal preview or sandbox operation.

## Risks and constraints

- YAAH tracks the `WindowsSandbox.exe` launcher, while the rendered host window belongs to `WindowsSandboxClient.exe`; client HWND discovery is separate and must be best-effort.
- DWM thumbnails are view-only and can fail due to session/elevation/rendering constraints.
- Tauri can force-kill its backend child on app close, so Python cleanup is not a guaranteed shutdown hook. Process teardown remains the OS-level fallback; phase 2 should add graceful cleanup where possible.
- Windows Sandbox is a desktop feature. Do not start a UI thread in headless service operation.

## Progress

- [x] Plan and phase boundaries recorded.
- [x] Phase 1 code and automated lifecycle tests implemented.
- [ ] Phase 1 interactive Windows Sandbox/DWM smoke test (requires a real sandbox window).
- [ ] Phase 2 lifecycle hardening.
- [ ] Phase 3 decision/prototype (not committed by default).
