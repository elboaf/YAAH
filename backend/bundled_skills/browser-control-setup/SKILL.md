---
name: browser-control-setup
description: Set up, verify, or repair YAAH's browser control (the Interceptor CLI) — check status, run or coach the one-click install, walk the user through loading the Chrome extension, and verify health. Use when browser commands fail, the user asks for browser control, automation setup, or asks "is browser control working?".
---

# Browser control setup (Interceptor)

YAAH's browser control is the Interceptor CLI: an agent-driven browser-only
build installed per-user from a pinned release. Setup has exactly two moving
parts — the CLI install and the Chrome extension load — and one genuinely
manual step (the extension, the user's job). This skill covers status, install,
coaching, and verification. For attaching files to web pages, load
`interceptor-file-attach` instead. For driving pages once set up, load the
junctioned `interceptor` / `interceptor-browser` packs (present only after
install).

## 1. Status first — never reinstall blind

Ask YAAH's backend, not the CLI directly, so the answer reflects what YAAH
knows (PATH augmentation, link ownership):

```
GET http://127.0.0.1:8765/api/interceptor/status
```

Desktop and dev runs bind 8765; a service deploy may override the port via
`YAAH_API_PORT` — read it from the environment when set. The response is the
whole picture:

| Field | Reads as |
|---|---|
| `installed` | `installed` / `not installed` (the CLI is found or not) |
| `version` | dotted CLI version, or null |
| `skill_links_summary` | `x/3` — junctions into `~/.yaah/skills` (router/browser/research) |
| `extension` | `ready` / `extension missing` |
| `extension_path` | the folder Chrome must load (present when installed) |
| `pinned` | the pinned-installer manifest: exact asset URL, SHA-256, license |

Decision table:

- `installed` = `not installed` → the Install button (or POST install, step 2)
  does the whole flow. Nothing exists yet — do not run `interceptor` commands.
- `installed` + `extension` = `extension missing` → the only remaining step is
  the user's extension load (step 3). Do not reinstall.
- `installed` + `extension` = `ready` → done. Verify health (step 4) only if
  asked.
- `skill_links_summary` under `3/3` after a successful install → the junctions
  are broken or were removed. There is no standalone relink route: POST the
  install endpoint again (idempotent — it repairs the YAAH-owned junctions and
  skips what is already correct), then check its returned status and `note`.

Every status read is a fresh scan plus one CLI diagnosis subprocess — cheap,
side-effect-free, and always current. Never cache a status answer across turns
if the user just acted on it.

## 2. Running the install

The install is user-initiated — never fire it because the agent wants browser
control; the user asks (or clicks Settings → Automate → Browser control →
Install) first. To run it for them, on request:

```
POST http://127.0.0.1:8765/api/interceptor/install
```

One call does: fetch the pinned release asset → verify SHA-256 against the
committed manifest → silent per-user install (no elevation) → junction the
three skill packs into `~/.yaah/skills` → write the done-marker (this is what
puts the install dir at the front of every tool-process PATH, so `interceptor`
resolves in the agent's very next shell — no YAAH restart).

Rules:

- **Safe to re-run.** Hash mismatch or 404 fails cleanly with no side effects;
  a rerun repairs or completes a partial state. Network required; there is no
  offline path.
- **Judge the POST's returned status, not the exit path** (see gotcha 5). Do
  not parse installer logs.
- **Never run the installer yourself** by downloading the exe or running
  `unins000.exe`'s siblings by hand — the backend's manager is the only
  installer surface; it pins, verifies, and records.
- **Uninstalling YAAH removes only YAAH's junctions.** Interceptor itself is
  left alone — an independently working app the user may keep.

## 3. Coaching the extension load (the one manual step)

The extension is an unpacked, manifest-keyed folder — its ID derives from the
manifest key, so nobody may copy or rewrite it; Chrome must load that exact
folder. The Settings panel shows these same steps with the real path and a
copy button; in chat, coach like this (the `extension_path` from status is the
folder — read it out or paste it, don't guess):

1. Open `chrome://extensions` and enable **Developer mode** (top-right).
2. Click **Load unpacked**.
3. Pick the folder from `extension_path`
   (default `%LOCALAPPDATA%\Programs\Interceptor\extension`).

Detection is the CLI's own diagnosis, not an assumption: after the user says
done, re-read status and confirm `extension` reads `ready` before declaring
victory. `ready` requires an empty `problems` + `blocked` arrays from
`interceptor diagnose extension --json`; any problem reads as
`extension missing` — judge the parsed JSON, never the command's exit code
(gotcha 5).

If the user skips the panel or never loads the extension: that's the valid
**partial setup** state (`installed` + `extension missing`), not an error to
fix. Reopening the panel or re-coaching later resumes; nothing is broken.
Browser commands will simply fail while the extension is missing — say that
plainly instead of retrying them.

## 4. Health verification ("is browser control healthy?")

Answer by command, never by session memory:

- Backend status (section 1) — the one-glance answer: installed + `3/3` links
  + `ready` extension = healthy.
- `interceptor status` — daemon mode, transport, link summary.
- `interceptor diagnose extension --json` — `{"problems": [], "blocked": []}`
  is the readiness contract (gaps name the exact gap: which profile is blocked,
  what permission is missing).

For a real end-to-end check on request: `interceptor open <url>` →
`interceptor read` → `interceptor tab close` — one background tab, open/read/
close, judged by parsed output.

## 5. Verified Windows gotchas

Empirically verified on the target machine (the session that validated this
install end-to-end). Don't re-derive them by failure:

1. **Absolute install path in fresh shells.** A freshly spawned shell may not
   see the PATH change until Windows broadcasts it; call the CLI by absolute
   path (`%LOCALAPPDATA%\Programs\Interceptor\interceptor.exe`) there. YAAH's
   own tool-process PATH augmentation (after a successful install) already
   handles the agent's shells — this bites only ad-hoc shells the agent
   spawns outside that channel.
2. **Screenshots: always `--format png`.** The local image viewer fails to
   sniff webp; a webp screenshot lands as an unreadable attachment.
3. **PowerShell 5.1 mangles large double-quoted native args** (embedded `""`
   escaping swallows trailing flags — the CLI then rejects with a confusing
   "group gate" error). For large payloads, use Git Bash, which quotes argv
   honestly (see `interceptor-file-attach` for the chunked-ferry recipe).
4. **`--any-tab` only with explicit user authorization.** Tabs outside
   Interceptor's managed group are off-limits by default; get a clear yes
   before touching foreign tabs.
5. **Exit codes on Windows are unreliable** (stderr progress banners can turn
   into `NativeCommandError` in PowerShell; the CLI's own exit codes have lied
   under both shells). Judge success by parsed output — the CLI's JSON, the
   page's own text — never by `$?` or `$LASTEXITCODE`.
