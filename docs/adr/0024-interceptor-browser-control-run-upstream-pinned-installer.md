# ADR 0024: Browser control ships by silently running Interceptor's own pinned installer, user-initiated only

Date: 2026-11-12
Status: Accepted
Driven by: Spec #630 ("Bake Interceptor browser control into YAAH"), settled in
the 2026-11-01 grilling session with the owner; ticket 7 of 8 (#637) records
it. Companion tickets: the Interceptor manager (t1–t4, #631–#634) and the
Settings surface (t5–t6, #635–#636).

## Context

YAAH agents can drive the user's real browser through the Interceptor CLI
(browser-only build), but that capability existed only as per-machine manual
setup: find and vet a third-party installer, run it, link three skill packs
into every agent runtime by hand, and walk the user through loading an
unpacked Chrome extension. A fresh YAAH install shipped no browser control and
every future machine repeated the busywork. The capability itself is proven —
a prior session validated the fork's Windows browser-only build end-to-end on
the target machine.

The decision to make is distribution: how does a third-party, ~55 MB Windows
binary with its own native-messaging registration and trust boundary get onto
user machines through a button click, without YAAH becoming a re-distributor
of someone else's payload?

## Decision

**YAAH runs upstream's own installer, pinned; the user initiates, always.**

1. **Run upstream's installer (pinned), never a repackaged payload.** Setup
   downloads the fork's browser-only Inno Setup installer
   (`Interceptor-Browser-2.1.6-windows-x64.exe`) from the elboaf/Interceptor
   GitHub releases, verifies its SHA-256 against a committed manifest
   (`backend/agent/interceptor_manifest.json`), and runs it silently
   (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`). The installer is per-user (no
   elevation), documents its silent mode, and owns the pieces YAAH must not
   reimplement: native-messaging registration, PATH, the uninstaller. The
   traffic-weight rationale: the payload is ~55 MB and a trust boundary —
   bytes YAAH would otherwise have to mirror, re-verify, and keep
   redistributable. Upstream's installer already carries that weight (and the
   Elastic License 2.0 terms, referenced in the manifest), so YAAH carries
   only the pin: a deliberate, reviewable one-line manifest bump is the only
   way what YAAH installs ever changes. YAAH never modifies Interceptor's
   payload after install — the extension ID derives from the extension
   manifest's `key` field, so byte-identity of that manifest is load-bearing.
   Building YAAH's own MSI/zip was rejected for exactly that re-distribution
   burden; wrapping `interceptor skills adopt` was rejected because it writes
   into other agent runtimes' skill folders, which YAAH must not touch.

2. **User-initiated only.** The Settings → Automate "Browser control" Install
   button is the only surface. No auto-install, no first-run prompt, no nag
   card — and therefore no suppression toggle (nothing exists to suppress).
   The agent can run or coach the same flow, but only on the user's request.

3. **Skill links are junctions into `~/.yaah/skills` only.** Post-install,
   YAAH junctions the three upstream packs (router / browser / research) into
   its own skills dir — links, not copies, so upstream updates flow through —
   idempotently, never overwriting a real directory, and never writing into
   `~/.claude` or `~/.agents`. A dangling junction (Interceptor uninstalled
   externally) simply drops out of YAAH's skill index.

4. **Leave-on-uninstall.** YAAH's uninstaller removes its three junctions and
   nothing else. Interceptor stays an independently working app. (As of
   #637, the junction cleanup is decided but not yet wired: the uninstaller
   has no hook into `remove_links()`, so junctions currently remain after
   an uninstall and can dangle if Interceptor is later removed — harmless,
   since the skills loader hides dangling entries. Wiring it in is tracked
   for the uninstaller side.)

Consequences: fresh installs fail cleanly when the pinned release is yanked
(hash mismatch or 404) and the fix is the one-line manifest bump; the feature
is Windows-only (the Linux build omits the section); fetched at setup time on
the user's machine, so YAAH's repo, build, and CI/release pipeline carry
nothing and change nothing.
