# yaah dev toolkit

This folder is mounted read/write into every Windows Sandbox yaah starts
(inside the VM: `C:\Users\WDAGUtilityAccount\Desktop\toolkit`). Because the
mount is read/write, **anything installed here survives VM disposal and is
inherited by every future sandbox** — it is the persistence mechanism.

## What yaah seeds here (do not delete)

- `bin\` — helper scripts, on PATH inside the VM already:
  - `vm-capture.ps1` — captures the VM's own screen to a PNG on the toolkit
    mount so the host (and a vision-capable model) can see it. THE tool for
    "something is wedged / a dialog is up / is my GUI app actually showing":
    `powershell -NoProfile -ExecutionPolicy Bypass -File
    <toolkit>\bin\vm-capture.ps1` (custom path: pass it as arg 1), then read
    the PNG from the toolkit's host side (e.g. `view_image` on the host path).
  - `git.cmd` — git shim for the MinGit layout (`mingit\cmd\git.exe`); also
    presets `GIT_CONFIG_GLOBAL` to this folder's `gitconfig` so mapped-workspace
    git stops failing on 'dubious ownership'. Drop MinGit into `toolkit\mingit`
    (or point the shim at any git.exe) and `git` works inside the VM.
  - `windows-mcp-serve.ps1` — starts the VENDORED windows-mcp MCP server
    (`vendor\windows_mcp`, MIT, from github.com/CursorTouch/Windows-MCP) inside
    the VM so the HOST can drive the sandbox GUI over HTTP as MCP tools
    (App/Snapshot/Click/Type/PowerShell/...). Run inside the VM:
    `powershell -ExecutionPolicy Bypass -File <toolkit>\bin\windows-mcp-serve.ps1`
    It installs pinned deps from `vendor\windows-mcp-requirements.txt` on first
    run (marker: `vendor\.windows-mcp-deps-ok`), then prints the host URL
    (`http://<vm-ip>:<port>/mcp` — NO trailing slash) and bearer key.
    Wire quirks (schemas are the truth, descriptions lie): App tool takes
    `mode=launch`/`mode=launch_executable` + `executable` (full path — Edge is
    NOT in the VM's start menu index), NOT `action`; Shortcut takes `shortcut`,
    NOT `keys`. Handshake: POST initialize -> grab `mcp-session-id` response
    header -> POST notifications/initialized -> then tools/list & tools/call,
    always sending the `mcp-session-id` header (an `/mcp/` trailing-slash POST
    307-redirects and silently DROPS the body).
- `gitconfig` — `[safe] directory = *` for the mapped-workspace SID mismatch.
- `state.json` — machine-readable manifest of what the toolkit contains
  (tools, versions, install/check commands). Check it BEFORE re-downloading
  anything: `Get-Content $env:...\toolkit\state.json` is one round-trip that
  can save a 200 MB reinstall.
- `INDEX.md` — human/agent-readable table of contents GENERATED from
  `state.json`. Read this one first. Never edit it by hand; it is
  regenerated on every manifest change.
- `bin\toolkit.ps1` (+ `bin\toolkit.cmd` shim) — the recording wrapper.
  Record every install through it instead of hand-editing state.json:

      toolkit install <name> -Version <v> -Kind zip -Path <rel\path> `
          -Check "Test-Path '<toolkit>\<rel\path>'" `
          -Invocation "<how to run it>" [-Note "<gotcha>"]

  Also `toolkit remove <name>` and `toolkit index` (regenerate INDEX.md).
  It updates state.json and then regenerates INDEX.md. `toolkit install`
  records
  the manifest entry only — the actual download/extract/shim work is still
  yours, and `-Check` is how the next session verifies it without
  rediscovery.

## What yaah does NOT ship

git/python/node/AutoHotkey/browsers are NOT bundled (download size). The VM is
a clean Windows image each boot; only this folder persists. Install what you
need INTO the toolkit (zip/portable distributions preferred — never run
interactive installers unattended), then record it with
`bin\toolkit.ps1 install <name> ...` so the next session (and every future
sandbox) knows it's there. Do not edit `state.json` by hand.

## Layout conventions (already on PATH inside the VM)

    toolkit\           <- zipped tools expand here
    toolkit\bin\       <- exe files and .cmd shims
    toolkit\Scripts\   <- python -m pip --target style pure-python trees
    toolkit\node_modules\.bin\

PATH order inside the VM: `toolkit; toolkit\bin; toolkit\Scripts;
toolkit\node_modules\.bin; <system>`.

`state.json` schema: `{"tools": {"<name>": {"version": "...", "kind":
"zip|dir|script|pip|installer|vendored", "path": "<rel path>",
"check": "<one-line PowerShell test>", "invocation": "<how to run>",
"note": "<gotcha>", "installed_at": "<iso8601>"}}}` — all metadata optional
except the name. yaah merges (never overwrites) the file when seeding a
fresh toolkit, so user-added entries survive app updates. Do not hand-edit:
record changes with `bin\toolkit.ps1` (`toolkit install|remove|index`), which
also regenerates `INDEX.md`.
