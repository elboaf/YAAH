# Windows Sandbox integration & the persistent dev toolkit

yaah can run its development-time verification inside **disposable Windows
Sandbox VMs** instead of assuming the host is a safe place to execute a
project under test. The model invokes this itself, at the moment live
verification beats code review: run the workspace's app, reproduce a bug,
exercise real behavior.

The tools:

| Tool            | Risk class | What it does                                            |
|-----------------|------------|---------------------------------------------------------|
| `sandbox_test`  | mutating   | Boot (or reuse) the sandbox; blocks until ready          |
| `sandbox_run`   | shell      | Run one PowerShell command inside the live sandbox       |
| `sandbox_status`| read       | Feature availability, enabled flag, session state        |
| `sandbox_stop`  | shell      | Dispose the sandbox (writes already persist)             |

Risk classes drive the access-mode gate (`PLAN-access-modes.md`): in `plan`
mode `sandbox_test` is blocked, in `ask` mode it prompts, and `sandbox_run`
is gated like `bash`.

## Architecture

```
HOST                                      SANDBOX VM
~/.yaah/toolkit/      ──R/W mount──►      C:\Users\WDAGUtilityAccount\Desktop\toolkit
<workspace>/          ──R/W mount──►      ...\Desktop\ws
~/.yaah/sandbox/<ws>-<hash>/logs/         ...\Desktop\logs
   ├─ yaah_sandbox_bootstrap.ps1           (LogonCommand target)
   ├─ cmd.<n>.ps1            ──────────►   executed by the bootstrap
   ├─ output.txt             ◄──────────   merged stdout+stderr
   ├─ res.<n>.json           ◄──────────   {exit_code, timed_out}
   └─ done.<n>               ◄──────────   ack marker
```

Windows Sandbox has **no host-side console API**, so yaah talks to the VM
through the mapped `logs` folder:

1. `sandbox_test` writes `sandbox.wsb` + the bootstrap script, launches
   `WindowsSandbox.exe <file>.wsb` detached, and polls `init.log` for the
   bootstrap's readiness marker (cold boot can take minutes; the first
   `sandbox_test` result for a workspace carries a note saying so).
2. `sandbox_run` drops `cmd.<n>.ps1` into the mapped dir (sequence numbers
   continue past anything already there, so a re-adopted session keeps
   counting). The bootstrap — a single PowerShell loop started by the
   `.wsb` `LogonCommand` — executes each command file exactly once (a
   processed-set guard), kills it at the `runtime.json` deadline, and
   writes back `output.txt`, `res.<n>.json`, and the `done.<n>` ack.
3. `sandbox_stop` kills the sandbox process tree. Nothing needs "exporting":
   everything written to an R/W-mapped folder **already persists on the
   host** after disposal.

### The persistent dev toolkit

`~/.yaah/toolkit` (config: `sandbox.toolkit_dir`) is mounted **read/write**
into every sandbox and prepended to the sandbox `PATH`
(`<toolkit>`, `<toolkit>\bin`, `<toolkit>\Scripts`,
`<toolkit>\node_modules\.bin`).

Because mapped-folder writes persist, the toolkit **grows naturally**:
a tool installed inside any sandbox (`winget` portable installs, `npm
install -g --prefix`, a script dropped in `bin\`) lands on the host the
moment it's written and is inherited by **every future sandbox** — no
snapshotting, no sync layer, no re-install per session.

Trade-off (accepted deliberately): R/W means a sandbox can also tamper
with existing toolkit files. The toolkit is a cache, not a trust boundary.

## Lifecycle & adoption

- One live session per app. Starting while one runs returns its info
  instead of spawning a second VM.
- A sandbox left running across an app restart is **re-adopted**: the
  session dir name is derived deterministically from the workspace path
  (`sha256`, not salted `hash()`), so after a restart the same mapped dir
  is rebuilt and a nonce handshake (`cmd.<pid><ts>.ps1` → `done.<pid><ts>`)
  proves the old bootstrap still watches it. If nothing acks, yaah refuses
  to spawn a second sandbox and says so.
- `sandbox_status` is safe to call anytime; it never blocks or spawns.

## Configuration (`config.json` → `sandbox`)

| Key               | Default             | Meaning                                        |
|-------------------|---------------------|------------------------------------------------|
| `enabled`         | `true`              | Master switch; `false` short-circuits the tools |
| `toolkit_dir`     | `~/.yaah/toolkit`   | Persistent toolkit mounted R/W into every VM    |
| `networking`      | `"Enable"`          | `.wsb` networking (`Enable` / `Disable`)        |
| `memory_mb`       | `8192`              | VM memory                                       |
| `vgpu`            | `"auto"`            | `.wsb` vGPU setting; `auto` = `Disable` on multi-GPU hosts (the 0x80072746 crash class, microsoft/Windows-Sandbox#64), `Default` otherwise; explicit `Default`/`Disable` wins |
| `map_workspace`   | `true`              | Mount the workspace R/W at `Desktop\ws`         |
| `startup_timeout` | `180`               | Seconds `sandbox_test` waits for first boot     |
| `auto_reboot_on_crash` | `true`         | One transparent `sandbox_test` reboot when the VM dies mid-session (0x80072746-class); in-VM state is lost, toolkit/workspace writes persist |

## Mid-session VM crash (0x80072746)

Windows Sandbox can die silently mid-session on some hosts (notably
multi-GPU machines with vGPU enabled): the host-sandbox connection is
forcibly closed and every subsequent `sandbox_run` would fail generically.
YAAH classifies this failure: the error names the crash class
(`0x80072746`), a diagnostics snapshot (`init.log` + last VmSwitch events)
is copied into the session `logs/crash-<timestamp>/` dir before the next
boot overwrites it, and — by default — one transparent reboot is attempted
via `sandbox_test`. The result carries `crashed: true` and a `reboot` note;
run the command again after a successful reboot.

## When the Windows feature is missing

`sandbox_test` returns error text the model relays to the user: check with
`Get-WindowsOptionalFeature -Online -FeatureName
'Containers-DisposableClientVM'`, enable with
`Enable-WindowsOptionalFeature -Online -FeatureName
'Containers-DisposableClientVM' -All` (elevated, **reboot required**) or
Settings → System → Optional features → More Windows features → Windows
Sandbox. Host-side `bash`/`powershell` keep working meanwhile.

## Manual smoke test (on a feature-enabled machine)

1. `python -m pytest backend/tests/test_sandbox.py -q` — must pass without
   the feature (fakes only).
2. In a yaah chat with a workspace that has a runnable app:
   ask for a live check, or call `sandbox_test` directly. Expect a sandbox
   window, then `{"status": "running", ...}` with the cold-start note on
   first boot.
3. `sandbox_run` `"$PSVersionTable.PSVersion.ToString()"` → output + exit 0.
4. `sandbox_run` `"npm install -g --prefix C:\Users\WDAGUtilityAccount\Desktop\toolkit <pkg>"`
   → after `sandbox_stop`, the package exists in host `~/.yaah/toolkit`.
   The next `sandbox_test` inherits it on `PATH`.
5. Close the app, leave the sandbox running, start yaah again,
   `sandbox_test` → note says "re-attached to the running sandbox".
