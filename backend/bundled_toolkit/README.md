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
- `gitconfig` — `[safe] directory = *` for the mapped-workspace SID mismatch.
- `state.json` — machine-readable manifest of what the toolkit contains
  (tools, versions, install/check commands). Check it BEFORE re-downloading
  anything: `Get-Content $env:...\toolkit\state.json` is one round-trip that
  can save a 200 MB reinstall.

## What yaah does NOT ship

git/python/node/AutoHotkey/browsers are NOT bundled (download size). The VM is
a clean Windows image each boot; only this folder persists. Install what you
need INTO the toolkit (zip/portable distributions preferred — never run
interactive installers unattended), then add a line to `state.json` so the
next session (and every future sandbox) knows it's there.

## Layout conventions (already on PATH inside the VM)

    toolkit\           <- zipped tools expand here
    toolkit\bin\       <- exe files and .cmd shims
    toolkit\Scripts\   <- python -m pip --target style pure-python trees
    toolkit\node_modules\.bin\

PATH order inside the VM: `toolkit; toolkit\bin; toolkit\Scripts;
toolkit\node_modules\.bin; <system>`.

`state.json` schema: `{"tools": {"<name>": {"version": "...", "kind":
"zip|dir|script", "check": "<one-line PowerShell test>", "installed_at":
"<iso8601>"}}}` — yaah merges (never overwrites) the file when seeding a
fresh toolkit, so user-added entries survive app updates.
