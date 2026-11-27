Fixes #747

Opened by the autonomous overnight agent.

## Task

Issue #747 proposed evaluating **cua-driver** (trycua/cua, MIT) as a way to
bring host-grade computer use back to YAAH without repeating ADR 0013's
failure mode (in-process `WH_*_LL` hooks serviced on a YAAH thread, starved by
test load -> system-wide cursor hitch).

## What the exploration found

cua-driver is a **third-party, out-of-process daemon** (not in-process hooks).
A client speaks MCP over a local socket -- the same pattern YAAH already uses
for windows-mcp in the sandbox. Because the daemon has its own process and
input session, the #299/#339 failure mode is structurally absent.

Key findings:

- **No-foreground contract** -- the agent observes any visible window and posts
  input via `SendInput`/`PostMessage` without stealing focus or moving the real
  cursor. Matches YAAH's "user present, watching, consent per session" boundary.
- **Permission modes chosen at launch** (`standard` / `bounded` / `unrestricted`),
  set via env vars read once at start. `bounded` fails startup instead of
  silently falling back. Maps onto YAAH's ask/allow + PLAN access modes.
- **Read-only first** (UIA tree + screen capture with set-of-marks), input
  actions a later phase.
- **Pinnable + Authenticode-signed** (Cua AI, Inc.); update mechanisms exist.

## Local evidence is now stale

The daemon that #747 was based on is **no longer on this machine**:
`Get-Process -Name cua-driver` returns nothing, no `cua-driver-serve` scheduled
task, and the binary at
`C:\Users\Administrator\AppData\Local\Programs\Cua\cua-driver\bin\cua-driver.exe`
is absent. So this is a **docs/source evaluation, not a prototype** -- the
"a daemon is already running, so a prototype is cheap" premise does not hold
today.

## The load-bearing decision

ADR 0013's context (in-process hooks starved by test load, YAAH-owned input
machinery) **does not apply** to a third-party, signed, out-of-process daemon
that runs opt-in, off by default, and never installs/starts anything from
YAAH. Two outcomes: close #747 as `wontfix`, or a follow-up ADR describing the
new boundary (read-only first). Both are spelled out in the doc.

Full analysis in `docs/research/cua-driver-host-computer-use.md`.

Labels: needs-triage
