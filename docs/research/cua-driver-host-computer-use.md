# cua-driver for host-grade computer use — exploration (issue #747)

Repo: https://github.com/trycua/cua
Inspected: web (repo README + cua.ai docs), 2026-11-27. No local clone — the
host evidence that motivated #747 (a running daemon) is no longer present on
this machine, so this is an evaluation from source/docs, not a prototype.

## TL;DR

cua-driver is a third-party, MIT-licensed, **out-of-process** daemon that gives
an agent read-only host observation (UIA tree + screen capture with set-of-
marks) and, in a later phase, input actions (click/type/scroll). Its
architecture structurally avoids ADR 0013's failure mode (in-process `WH_*_LL`
hooks serviced on a YAAH thread). It is opt-in, off by default, and the
"no-foreground" contract matches YAAH's "the user is watching, consent per
session" boundary. The maintainer's gate (ADR 0013 context still applies?) is
the load-bearing decision; everything else is a follow-up ADR if the answer is
yes.

## What cua-driver is (verified from source/docs)

- **Out-of-process daemon**, not in-process hooks. `cua-driver serve` runs as a
  separate background process; clients speak MCP over a local socket (the
  windows-mcp precedent already exists in YAAH's sandbox). Nothing is hooked
  into YAAH's process; nothing is serviced on a YAAH thread. The #299 failure
  mode (hook thread starved by a pytest run → system-wide cursor hitch) is
  structurally absent — the daemon has its own thread(s) and its own input
  session contract.
- **No-foreground contract.** The agent can observe any visible window and post
  synthesized input (`SendInput` / `PostMessage`) without stealing focus,
  moving the real cursor, or switching virtual desktops. A separate overlay
  cursor shows where the agent acts. This matches YAAH's "user present,
  watching, consent per session" use case and does not collide with the
  sandbox's own input session.
- **Cross-platform** (macOS AppKit / Windows UIA + input / Linux). Windows stack:
  UIAutomation tree reading + `SendInput`/`PostMessage` dispatch.
- **Permission modes chosen at launch** (immutable for the process lifetime, set
  via env vars read once at start):
  - `standard` — client-side approvals (default).
  - `bounded` — a reviewed capability manifest, fails closed. Set via
    `CUA_DRIVER_PERMISSION_MODE=bounded`,
    `CUA_DRIVER_CAPABILITY_MANIFEST_FILE`, and
    `CUA_DRIVER_CAPABILITY_MANIFEST_APPROVED=1`. A bad config **fails startup**
    instead of silently falling back — a safe default posture.
  - `unrestricted` — full input.
- **Optional screen capture with set-of-marks overlays** (numbered elements),
  trajectory recording/replay, and health/diagnostics (`health_report`).
- **Actively maintained.** 33k stars, MIT, weekly Windows fixes as of Nov 2026.
  Latest release `bench 0.4.25` dated 2026-11-27. A `cua-driver check-update`
  mechanism and canonical installer exist (`cua-driver update --apply`).

## How it maps to YAAH

| YAAH concern | cua-driver fit |
| --- | --- |
| ADR 0013: "out of the business of touching the host desktop" | Daemon is YAAH-owned only as a *client*. YAAH never installs, starts, or ships the binary. The user points YAAH at a daemon they already run (mirrors the sandbox/windows-mcp split). |
| #299/#339: in-process hooks starved under load | Out-of-process. No hooks in YAAH's process, nothing serviced on a YAAH thread. The tripwire `backend/tests/test_issue_339_no_input_hooks.py` stays green — no `backend.agent.computer` module, no hook libs in `sys.modules`, no host tools in the registry. A daemon client is a different thing from in-process hooks. |
| ask/allow machinery / PLAN access modes | Every action ask-gated, like shell/remote today. Permission modes map onto YAAH's consent model: read-only = default, input actions require per-session consent. |
| "the user is watching" consent | The no-foreground contract + per-session consent. Input actions restore focus/cursor transiently (macOS) or not at all (Windows) and report whether they were restored. |
| Trust posture (vendor binary) | Pinnable version; signature check at startup is feasible (the daemon is Authenticode-signed by "Cua AI, Inc."). Bundled-vs-user-installed is a decision. |

## Local evidence on this machine (now stale)

The investigation #747 was based on (2026-11-09) no longer holds:

- **No daemon running.** `Get-Process -Name cua-driver` returns nothing.
- **No scheduled task.** `Get-ScheduledTask | ? TaskName -like *cua*` returns
  nothing. The `cua-driver-serve` task that Hermes Agent once installed is
  absent.
- **No binary.** `C:\Users\Administrator\AppData\Local\Programs\Cua\cua-driver\
  bin\cua-driver.exe` (v0.23.2, Authenticode-signed) is not present.

The machine was likely reset or Hermes Agent uninstalled the driver since the
original write-up. So this is a **docs/source evaluation, not a prototype** —
the "a daemon is already running, so a prototype is cheap" premise does not
apply today. A prototype would require reinstalling the daemon.

## The decision the maintainer needs to make

ADR 0013's context was: in-process hooks starved by YAAH's own test load, with
YAAH-owned input machinery on the desktop. That context **does not apply** to a
third-party, signed, out-of-process daemon that:

- runs whether or not YAAH is running (opt-in, off by default),
- is gated by YAAH's existing permission modes (every action ask-gated),
- requires explicit "the user is watching" consent per session,
- never installs or starts anything host-side from YAAH.

Two outcomes:

1. **No** → close #747 as `wontfix`, leave a pointer in ADR 0013.
2. **Yes** → a follow-up ADR describing the new boundary: YAAH never installs
   or starts anything host-side; the user opts in by pointing YAAH at a running
   daemon (mirroring the sandbox/windows-mcp split). Read-only first
   (capture + UIA tree) with input actions a separate later phase.

## Open questions (from #747, still open)

1. Does the maintainer want host computer use at all, given ADR 0013? (Gate.)
2. Read-only first, input actions later? (Recommended.)
3. Transport: speak MCP directly to the daemon (windows-mcp precedent) or via a
   thin wrapper?
4. Trust: pin a version; signature check at startup; bundled vs user-installed?
5. Interaction with the sandbox workflow: keep the sandbox for untrusted/
   disruptive GUI work and reserve cua-driver for "on the real desktop, user
   present" work.
6. What does `test_issue_339_no_input_hooks.py` need to allow? A daemon client
   is not in-process hooks, but the tripwire's grep would need a carve-out only
   if YAAH imports a cua client library — a thin subprocess client (MCP over a
   socket) needs no carve-out.

## Suggested next step (if the answer to Q1 is yes)

Record the boundary in a follow-up ADR (read-only first), then a prototype that
spawns `cua-driver serve` as a subprocess and reads the UIA tree over MCP —
mirroring the existing sandbox/windows-mcp pattern, but on the real desktop
instead of the VM.
