"""Windows Sandbox integration: disposable test VMs + a persistent toolkit.

The model calls `sandbox_test` to run the workspace's app, tests and
builds inside a disposable VM (the default place to run things):
run the workspace's app, reproduce a bug, exercise real behavior. That
starts (or reuses) a Windows Sandbox VM built from a generated `.wsb`
config that maps three host folders in:

  workspace  -> C:\\Users\\WDAGUtilityAccount\\Desktop\\ws      (R/W)
  toolkit    -> C:\\Users\\WDAGUtilityAccount\\Desktop\\toolkit (R/W)
  logs       -> C:\\Users\\WDAGUtilityAccount\\Desktop\\logs    (R/W)

Windows Sandbox has no host-side console API for a running VM, so tool
I/O rides the mapped `logs` folder: the host drops `cmd.<n>.ps1` files, a
PowerShell bootstrap (started by the .wsb LogonCommand) executes them and
writes `output.txt` + `res.<n>.json` + `done.<n>` back. Mapped folders
mount before LogonCommand runs, and writes to an R/W-mapped folder
persist on the host after the sandbox is disposed — which is the entire
persistence story for the toolkit: anything a sandbox installs there is
immediately on the host and inherited by every future sandbox.

Latency is ~1-3s per command (file polling), so results tell the model
to batch commands. One live session per app; the session is host-side
state, and a sandbox left running across an app restart is re-adopted
via a nonce handshake when its bootstrap is still watching our log dir.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from backend.agent import config as config_mod

# Fixed mount points inside the sandbox (the container user is always
# WDAGUtilityAccount). The bootstrap template depends on these constants.
SB_WS = r"C:\Users\WDAGUtilityAccount\Desktop\ws"
SB_TOOLKIT = r"C:\Users\WDAGUtilityAccount\Desktop\toolkit"
SB_LOGS = r"C:\Users\WDAGUtilityAccount\Desktop\logs"
BOOTSTRAP_NAME = "yaah_sandbox_bootstrap.ps1"

# Module-level knob so tests run at millisecond polling speed.
POLL_INTERVAL = 0.5
# How long start_sync waits for the adoption nonce handshake before
# concluding a running sandbox is not ours.
ADOPT_TIMEOUT = 30

MAX_RUN_TIMEOUT = 900

# How long a second caller waits for the sandbox mutex before getting a
# busy error (sandbox.busy_wait_seconds; the config value overrides).
BUSY_WAIT_DEFAULT = 10
MAX_OUTPUT_CHARS = 20_000

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_ENABLE_NUDGE = (
    "Windows Sandbox is not available on this machine. The user can check "
    "its state with (elevated) PowerShell: Get-WindowsOptionalFeature "
    "-Online -FeatureName 'Containers-DisposableClientVM', and enable it "
    "with: Enable-WindowsOptionalFeature -Online -FeatureName "
    "'Containers-DisposableClientVM' -All  (elevated; a reboot is "
    "required), or via Settings > System > Optional features > More "
    "Windows features > tick 'Windows Sandbox'. Relay this to the user "
    "and continue without the sandbox; host-side bash/powershell still "
    "work."
)

# Mirrors config.DEFAULTS["sandbox"] for local deep-merging: config.load_config
# replaces nested dicts wholesale, so a user config with a partial "sandbox"
# section must not lose the unset keys.
_SANDBOX_DEFAULTS = {
    "enabled": True,
    "toolkit_dir": "",
    "networking": "Enable",
    "memory_mb": 8192,
    # issue #27 sandbox mutex: how long a second chat's sandbox call waits
    # for the single-instance VM before getting a busy error.
    "busy_wait_seconds": BUSY_WAIT_DEFAULT,
    # "auto" (issue #27): Disable on multi-GPU hosts (the vGPU crash class),
    # Default otherwise. Explicit "Default"/"Disable" wins.
    "vgpu": "auto",
    "map_workspace": True,
    "startup_timeout": 180,
    # One transparent sandbox_test reboot on detected mid-session VM death
    # (0x80072746-class crash); state loss is fine for a disposable VM.
    "auto_reboot_on_crash": True,
}


# ---------------------------------------------------------------- config / paths

def _cfg() -> dict:
    raw = (config_mod.load_config().get("sandbox") or {})
    return {**_SANDBOX_DEFAULTS, **(raw or {})}


def toolkit_dir() -> Path:
    """The persistent dev toolkit. Created lazily on first sandbox start."""
    raw = str(_cfg().get("toolkit_dir") or "").strip()
    p = Path(raw).expanduser() if raw else Path.home() / ".yaah" / "toolkit"
    return p


def bundled_toolkit_source() -> Path | None:
    """Directory holding the toolkit baseline shipped with the app
    (backend/bundled_toolkit in the repo; <exe>/_up_/backend/
    bundled_toolkit in the installed layout — same _up_ convention as
    bundled_skills, see skills.bundled_source_dir)."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).parent
        candidates += [exe / "_up_" / "backend" / "bundled_toolkit",
                       exe / "bundled_toolkit",
                       Path.cwd() / "backend" / "bundled_toolkit"]
    candidates.append(Path(__file__).parent.parent / "bundled_toolkit")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def ensure_toolkit_seed() -> list[str]:
    """Seed the toolkit with yaah's shipped baseline (helper scripts,
    gitconfig, README, state manifest) so a FRESH INSTALL works out of the
    box — before the first VM ever boots. Copy-once semantics: an existing
    file is never overwritten, so tools the user (or a sandbox session)
    installed into the toolkit survive app upgrades. state.json is merged
    instead: bundled entries are added, user-added entries are kept.
    Returns the list of files written (empty when everything was present)."""
    written: list[str] = []
    src = bundled_toolkit_source()
    if src is None:
        return written
    tk = toolkit_dir()

    def _copy(rel: str) -> None:
        s, t = src / rel, tk / rel
        if t.exists():
            return
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, t)
            written.append(rel)
        except OSError:
            pass  # best-effort; the VM is still perfectly usable

    try:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                _copy(p.relative_to(src).as_posix())
    except OSError:
        return written

    # Merge the bundled state.json entries into an existing toolkit manifest
    # (copy-once above skips it when present, but user tools must survive).
    state = tk / "state.json"
    try:
        bundled = json.loads((src / "state.json").read_text(encoding="utf-8"))
        current = (json.loads(state.read_text(encoding="utf-8"))
                   if state.exists() else {})
        tools = dict(current.get("tools") or {})
        for name, meta in (bundled.get("tools") or {}).items():
            if name not in tools:
                tools[name] = meta
        if tools != (current.get("tools") or {}):
            current["tools"] = tools
            state.write_text(
                json.dumps(current, indent=2) + "\n", encoding="utf-8")
            if "state.json" not in written:
                written.append("state.json")
    except (OSError, ValueError):
        pass
    return written


def _base_dir() -> Path:
    """Host-side scratch for wsb files + mapped command dirs."""
    return Path.home() / ".yaah" / "sandbox"


def session_dir(workspace: str) -> Path:
    """Per-workspace session dir. The digest is deterministic (NOT salted
    hash()) so a sandbox left running across an app restart maps the same
    dir name and the adoption handshake can find it."""
    from backend.agent.tools import workspace_root

    import hashlib

    root = str(workspace_root(workspace))
    if root == str(Path.home().resolve()):
        slug = "home"
    else:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(root).name)[:40].strip("_") or "ws"
    digest = hashlib.sha256(root.lower().encode("utf-8")).hexdigest()[:10]
    return _base_dir() / f"{slug}-{digest}"


# ---------------------------------------------------------------- availability

def sandbox_exe() -> Path | None:
    """Path to WindowsSandbox.exe, or None when the feature is missing."""
    override = os.environ.get("YAAH_SANDBOX_EXE")
    if override:
        p = Path(override)
        return p if p.exists() else None
    if os.name != "nt":
        return None
    candidate = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "WindowsSandbox.exe"
    )
    return candidate if candidate.exists() else None


def sandbox_available() -> bool:
    return sandbox_exe() is not None


def _gpu_adapter_count() -> int:
    """Number of display adapters on the host (best effort; 1 if unknown).
    Used by vgpu='auto': >1 adapter is the 0x80072746 crash class."""
    if os.name != "nt":
        return 1
    try:
        out = subprocess.run(
            ["wmic", "path", "Win32_VideoController", "get", "Name", "/NH"],
            capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
            timeout=15,
        ).stdout
    except Exception:  # noqa: BLE001
        return 1
    return len([ln for ln in out.splitlines() if ln.strip()]) or 1


def effective_vgpu() -> str:
    """Resolve the vGPU setting for wsb generation. 'auto' disables the
    paravirtualized GPU on multi-GPU hosts (microsoft/Windows-Sandbox#64:
    vGPU sandboxes crash ~30-120 s after boot with 0x80072746 there) and
    keeps Default otherwise. An explicit Default/Disable passes through."""
    raw = str(_cfg().get("vgpu") or "Default")
    if raw.lower() == "auto":
        return "Disable" if _gpu_adapter_count() > 1 else "Default"
    return raw


def _snapshot_crash_diagnostics(logs: Path) -> str | None:
    """On suspected mid-session VM death, copy the crash evidence into the
    session logs dir before the next boot overwrites init.log, and return a
    short evidence summary for the error text. Best effort throughout."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap_dir = logs / f"crash-{stamp}"
    lines = [f"sandbox crash diagnostics snapshot: {snap_dir}"]
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"crash diagnostics unavailable ({e})"
    for name in ("init.log", "output.txt"):
        src = logs / name
        if src.exists():
            try:
                (snap_dir / name).write_text(
                    src.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8")
                lines.append(f"copied {name}")
            except OSError as e:
                lines.append(f"could not copy {name}: {e}")
    if os.name == "nt":
        # Last VmSwitch NIC events: the issue's evidence table showed the
        # sandbox NIC disconnect+delete at the moment of death.
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-"
                 "Hyper-V-VmSwitch-Admin'; StartTime="
                 "(Get-Date).AddHours(-1)} -MaxEvents 20 -ErrorAction "
                 "Stop | Select-Object TimeCreated, Id | Format-Table -Hide"
                 "TableHeaders | Out-String"],
                capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
                timeout=30,
            )
            events = (out.stdout or "").strip()
            if events:
                (snap_dir / "vmswitch-events.txt").write_text(
                    events, encoding="utf-8")
                lines.append("copied last VmSwitch events")
        except Exception:  # noqa: BLE001
            lines.append("VmSwitch event query failed (best effort)")
    lines.append("note: init.log in the snapshot is from the crashed boot; "
                 "the next sandbox_test overwrites the live copy")
    return "; ".join(lines)


# ---------------------------------------------------------------- wsb generation

def _esc(path: Path) -> str:
    return _xml_escape(str(path))


def generate_wsb(workspace_root: Path, toolkit: Path, logs: Path) -> str:
    """The .wsb XML. Mapped folders mount before LogonCommand runs; R/W
    writes to them persist on the host after disposal (toolkit mechanism)."""
    cfg = _cfg()
    folders = [
        (toolkit, SB_TOOLKIT),
        (logs, SB_LOGS),
    ]
    if cfg.get("map_workspace", True):
        folders.insert(0, (workspace_root, SB_WS))
    mapped = "".join(
        f"    <MappedFolder>\n"
        f"      <HostFolder>{_esc(h)}</HostFolder>\n"
        f"      <SandboxFolder>{_xml_escape(s)}</SandboxFolder>\n"
        f"      <ReadOnly>false</ReadOnly>\n"
        f"    </MappedFolder>\n"
        for h, s in folders
    )
    logon = _xml_escape(
        f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle '
        f'Hidden -File "{SB_LOGS}\\{BOOTSTRAP_NAME}"'
    )
    return (
        "<Configuration>\n"
        f"  <Networking>{cfg.get('networking', 'Enable')}</Networking>\n"
        f"  <MemoryInMB>{int(cfg.get('memory_mb') or 8192)}</MemoryInMB>\n"
        f"  <vGPU>{_xml_escape(effective_vgpu())}</vGPU>\n"
        f"  <MappedFolders>\n{mapped}  </MappedFolders>\n"
        f"  <LogonCommand>\n    <Command>{logon}</Command>\n"
        f"  </LogonCommand>\n"
        "</Configuration>\n"
    )


def _bootstrap_script() -> str:
    """Runs inside the sandbox (started by LogonCommand). Prepends the
    toolkit to PATH, signals readiness on init.log, then polls the mapped
    logs dir for cmd.<n>.ps1 files and executes them one at a time,
    writing output.txt + res.<n>.json + done.<n> back for the host."""
    return f"""$ErrorActionPreference = 'Continue'
$dir = '{SB_LOGS}'
$tk  = '{SB_TOOLKIT}'
$ws  = '{SB_WS}'
$processed = @{{}}
$env:Path = "$tk;$tk\\bin;$tk\\Scripts;$tk\\node_modules\\.bin;$env:Path"
# git must never open an interactive editor inside the VM: the command
# channel would hang until the host-side timeout (an editor-less `git
# commit` must fail fast with 'empty message' instead). true.exe ships
# with Git for Windows; exit 0 also satisfies --amend and rebase --continue.
$env:GIT_EDITOR = 'C:\\Program Files\\Git\\usr\\bin\\true.exe'
$env:EDITOR = 'C:\\Program Files\\Git\\usr\\bin\\true.exe'
$env:VISUAL = 'C:\\Program Files\\Git\\usr\\bin\\true.exe'
$cwd = $dir
if (Test-Path $ws) {{ $cwd = $ws }}
Set-Location $cwd
# Set-Location only moves the PS provider location; child processes inherit
# the PROCESS cwd (system32 otherwise), so pin that too.
[Environment]::CurrentDirectory = $cwd
# UTF-8 wrapper: invoking the command file with the call operator (&)
# keeps `exit N` semantics (verified: propagates N; a native-command tail
# propagates $LASTEXITCODE) while forcing UTF-8 stdio both ways.
[IO.File]::WriteAllText("$dir\\__yaah_wrapper.ps1", @'
param([string]$ScriptPath)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
& $ScriptPath
if ($null -ne $LASTEXITCODE) {{ exit $LASTEXITCODE }}
'@, [System.Text.Encoding]::UTF8)
# The VM is disposable and sits behind the host's NAT — nothing inside it
# is worth protecting, and the Defender allow-dialog for a freshly bound
# port would stall the session exactly like an installer wizard. The
# sandbox user is admin without UAC, so this needs no elevation
# (verified live: all three profiles go OFF, exit 0).
netsh advfirewall set allprofiles state off | Out-Null
# Auto-start the vendored windows-mcp GUI server (the PRIMARY GUI control
# layer: the host drives the VM's GUI over MCP/HTTP). Detached so a slow
# first-run pip install never delays the ready signal; failure is
# non-fatal (the session can start it manually via
# toolkit\\bin\\windows-mcp-serve.ps1).
Start-Process -FilePath "$PSHOME\\powershell.exe" `
  -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"$tk\\bin\\windows-mcp-serve.ps1" `
  -WindowStyle Hidden
# Connection info for the host (readable from the mapped logs dir).
$mcpIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object {{ $_.IPAddress -notmatch '^127\\.|^169\\.254\\.' }} |
  Select-Object -First 1).IPAddress
$mcpKey = if ($env:WMCP_KEY) {{ $env:WMCP_KEY }} else {{ 'sandbox-demo-key' }}
$mcpPort = if ($env:WMCP_PORT) {{ $env:WMCP_PORT }} else {{ '8000' }}
('{{' + '"url": "http://' + $mcpIp + ':' + $mcpPort + '/mcp", "auth": "Bearer ' + $mcpKey + '"}}') |
  Out-File -FilePath "$dir\\mcp.json" -Encoding utf8
"[$(Get-Date -Format o)] yaah-sandbox-ready" | Out-File -FilePath "$dir\\init.log" -Encoding utf8
while ($true) {{
  $pending = Get-ChildItem -Path $dir -Filter 'cmd.*.ps1' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime
  foreach ($f in $pending) {{
    if ($processed.ContainsKey($f.Name)) {{ continue }}
    $processed[$f.Name] = 1
    if ($f.Name -match '^cmd\\.(\\d+)\\.ps1$') {{
      $n = $Matches[1]
      $tmo = 900
      try {{
        $rt = Get-Content -Raw "$dir\\runtime.json" | ConvertFrom-Json
        if ($rt.command_timeout_seconds) {{ $tmo = [int]$rt.command_timeout_seconds }}
      }} catch {{}}
      Remove-Item "$dir\\done.$n" -ErrorAction SilentlyContinue
      Remove-Item "$dir\\res.$n.json" -ErrorAction SilentlyContinue
      $psi = New-Object System.Diagnostics.ProcessStartInfo
      $psi.FileName = "$PSHOME\\powershell.exe"
      $psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$dir\\__yaah_wrapper.ps1`" -ScriptPath `"$($f.FullName)`""
      $psi.UseShellExecute = $false
      $psi.CreateNoWindow = $true
      # Pin the command's cwd to the workspace mount (the documented
      # sandbox cwd); the bootstrap's own process cwd is system32.
      $psi.WorkingDirectory = $cwd
      $psi.RedirectStandardOutput = $true
      $psi.RedirectStandardError = $true
      $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
      $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
      $p = [System.Diagnostics.Process]::Start($psi)
      # Drain both pipes concurrently: sequential ReadToEnd deadlocks when
      # stdout fills the pipe buffer while we are blocked reading stderr.
      $ot = $p.StandardOutput.ReadToEndAsync()
      $et = $p.StandardError.ReadToEndAsync()
      $timedOut = $false
      if (-not $p.WaitForExit($tmo * 1000)) {{
        $timedOut = $true
        try {{ $p.Kill() }} catch {{}}
        $p.WaitForExit()
      }}
      $dl = [System.DateTime]::UtcNow.AddSeconds(5)
      while ((-not $ot.IsCompleted -or -not $et.IsCompleted) -and
             [System.DateTime]::UtcNow -lt $dl) {{ Start-Sleep -Milliseconds 50 }}
      $o = ''; $e = ''
      if ($ot.IsCompleted) {{ try {{ $o = $ot.Result }} catch {{}} }}
      if ($et.IsCompleted) {{ try {{ $e = $et.Result }} catch {{}} }}
      [IO.File]::WriteAllText("$dir\\output.txt", "$o$e", [System.Text.Encoding]::UTF8)
      $meta = @{{ exit_code = $p.ExitCode; timed_out = $timedOut }} | ConvertTo-Json -Compress
      # ASCII: the payload is pure ASCII and avoids a BOM that would
      # break the host's json.loads; output.txt stays UTF-8 on purpose.
      [IO.File]::WriteAllText("$dir\\res.$n.json", $meta, [System.Text.Encoding]::ASCII)
      [IO.File]::WriteAllText("$dir\\done.$n", '1')
    }}
  }}
  Start-Sleep -Milliseconds 500
}}
"""


# ---------------------------------------------------------------- session state

# One live sandbox per app: {"proc": Popen|None, "pid": int|None,
# "dir": Path, "logs": Path, "workspace": str, "adopted": bool}
_SESSION: dict | None = None
_lock = threading.Lock()
# Workspace of the last started session, kept across a crash-clear so the
# one-shot auto reboot (issue #27) knows what to restart.
_LAST_WORKSPACE: str | None = None


def _set_last_workspace(workspace: str | None) -> None:
    global _LAST_WORKSPACE
    _LAST_WORKSPACE = workspace


def _alive() -> bool:
    if not _SESSION:
        return False
    proc = _SESSION.get("proc")
    if proc is None:
        pids = _sandbox_pids()
        pid = _SESSION.get("pid")
        return bool(pid and pid in pids)
    return proc.poll() is None


def _sandbox_pids() -> list[int]:
    """PIDs of every running WindowsSandbox.exe (best effort; [] on POSIX)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WindowsSandbox.exe",
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
            timeout=15,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "windowssandbox.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def _spawn(exe: Path, wsb: Path, logs: Path):
    """Launch the sandbox. Split out so tests can fake the VM side."""
    return subprocess.Popen(
        [str(exe), str(wsb)], creationflags=_CREATE_NO_WINDOW, close_fds=True,
    )


def _clean_logs(logs: Path) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    for pat in ("cmd.*.ps1", "done.*", "res.*.json", "output.txt",
                "out.tmp", "err.tmp", "runtime.json"):
        for f in logs.glob(pat):
            try:
                f.unlink()
            except OSError:
                pass


def _next_seq(logs: Path) -> int:
    """Command sequence continues past any files already in the mapped dir
    (a re-adopted sandbox still holds older cmd files with their acks)."""
    top = 0
    for f in logs.glob("cmd.*.ps1"):
        m = re.match(r"cmd\.(\d+)\.ps1$", f.name)
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def _write_session_files(sdir: Path, workspace_root: Path) -> tuple[Path, Path, Path]:
    toolkit = toolkit_dir()
    toolkit.mkdir(parents=True, exist_ok=True)
    # Baseline seeding runs on EVERY boot (not just the first): a fresh
    # install gets its toolkit baseline here, and an upgraded app ships new
    # baseline files the same way. Copy-once per file — user-modified or
    # session-installed content is never overwritten.
    ensure_toolkit_seed()
    logs = sdir / "logs"
    _clean_logs(logs)
    (sdir / "sandbox.wsb").write_text(
        generate_wsb(workspace_root, toolkit, logs), encoding="utf-8")
    # The bootstrap must live INSIDE the mapped logs dir: the .wsb
    # LogonCommand targets it there, and the session dir itself is not
    # mapped into the VM at all.
    (logs / BOOTSTRAP_NAME).write_text(_bootstrap_script(), encoding="utf-8")
    (logs / "runtime.json").write_text(
        json.dumps({"command_timeout_seconds": MAX_RUN_TIMEOUT}),
        encoding="utf-8")
    return sdir / "sandbox.wsb", logs / BOOTSTRAP_NAME, logs


# ---------------------------------------------------------------- lifecycle

def start_sync(workspace: str) -> dict:
    """Start (or reuse) the sandbox for this workspace. Blocking; executors
    run it via asyncio.to_thread so the event loop never stalls."""
    global _SESSION
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {
            "error": "the sandbox feature is disabled in yaah's config "
                     "(config.json: sandbox.enabled = true re-enables it)"
        }
    exe = sandbox_exe()
    if exe is None:
        return {"error": _ENABLE_NUDGE}

    if _alive():
        return _session_info("sandbox already running")

    # Booting (or adopting) mutates the shared session; serialize it the
    # same way commands are. A concurrent boot keeps the second caller
    # waiting only for busy_wait_seconds before a busy error.
    wait = _busy_wait_seconds()
    if not _lock.acquire(timeout=wait):
        return {"error": _BUSY_ERROR.format(wait=wait), "busy": True}
    try:
        if _alive():
            return _session_info("sandbox already running")

        sdir = session_dir(workspace)
        from backend.agent.tools import workspace_root

        ws_root = workspace_root(workspace)
        wsb, _boot, logs = _write_session_files(sdir, ws_root)

        # A sandbox left running from a previous app session: adopt it only if
        # its bootstrap answers a nonce handshake on OUR mapped dir (proving
        # it watches this session's files). Otherwise refuse a second VM.
        if _sandbox_pids():
            if _try_adopt(logs):
                _SESSION = {"proc": None, "pid": None, "dir": sdir, "logs": logs,
                            "workspace": workspace, "adopted": True}
                _set_last_workspace(workspace)
                return _session_info("re-attached to the running sandbox")
            return {
                "error": "a Windows Sandbox is already running but is not "
                         "answering yaah's command channel (it maps a different "
                         "workspace, or was started by hand). Close it and call "
                         "sandbox_test again."
            }

        proc = _spawn(exe, wsb, logs)
        _SESSION = {"proc": proc, "pid": proc.pid, "dir": sdir, "logs": logs,
                    "workspace": workspace, "adopted": False}
        _set_last_workspace(workspace)

        deadline = time.time() + max(15, int(cfg.get("startup_timeout") or 180))
        while time.time() < deadline:
            if proc.poll() is not None:
                _SESSION = None
                return {"error": f"Windows Sandbox exited during startup "
                                 f"(exit code {proc.poll()}); check the "
                                 f"Windows Sandbox feature is fully enabled"}
            init = logs / "init.log"
            if init.exists():
                try:
                    if "yaah-sandbox-ready" in init.read_text(
                            encoding="utf-8", errors="replace"):
                        return _session_info()
                except OSError:
                    pass
            time.sleep(POLL_INTERVAL)
        # Leave the VM alone (it may still be booting for the user); the next
        # sandbox_test/sandbox_status call re-checks and can adopt it.
        return {"error": f"the sandbox did not signal readiness within "
                         f"{int(cfg.get('startup_timeout') or 180)}s (first "
                         f"boot can be slow); try sandbox_status in a moment"}
    finally:
        _lock.release()


def _try_adopt(logs: Path) -> bool:
    """One no-op command with a random name; an ack on the matching done
    file proves a bootstrap watches this exact mapped dir."""
    nonce = f"{os.getpid()}{int(time.time() * 1000) % 100000}"
    cmd = logs / f"cmd.{nonce}.ps1"
    done = logs / f"done.{nonce}"
    try:
        done.unlink()
    except OSError:
        pass
    cmd.write_text("Write-Output 'yaah-adopt-ok'", encoding="utf-8")
    deadline = time.time() + ADOPT_TIMEOUT
    while time.time() < deadline:
        if done.exists():
            # Sequence continues past anything already in the dir.
            for leftover in (cmd, done, logs / f"res.{nonce}.json",
                             logs / "output.txt"):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            return True
        time.sleep(POLL_INTERVAL)
    try:
        cmd.unlink()
    except OSError:
        pass
    return False


def _start_preview() -> None:
    """Show the optional interactive Windows preview without blocking startup."""
    if os.name != "nt" or os.environ.get("YAAH_HEADLESS") == "1":
        return
    try:
        from backend.agent import sandbox_preview

        sandbox_preview.start_preview()
    except Exception:  # preview is never required for sandbox
        logging.getLogger(__name__).exception("could not start sandbox preview")


def _stop_preview() -> None:
    """Best-effort release of the optional sandbox preview."""
    try:
        from backend.agent import sandbox_preview

        sandbox_preview.stop_preview()
    except Exception:  # preview cleanup must not block stop
        logging.getLogger(__name__).exception("could not stop sandbox preview")


def _session_info(note: str | None = None) -> dict:
    # Centralized successful-session path covers new boots, reuse, and adopt.
    _start_preview()
    s = _SESSION or {}
    first_marker = s.get("dir", Path()) / "launched_once"
    note_text = note
    if not first_marker.exists():
        note_text = (note_text or "") + (
            " first sandbox boot for this workspace is a cold start (can "
            "take a couple of minutes); tell the user the sandbox window "
            "opening on their desktop is expected."
        ).strip()
        try:
            first_marker.write_text("1", encoding="utf-8")
        except OSError:
            pass
    info = {
        "status": "running",
        "sandbox_paths": {
            "workspace": SB_WS,
            "toolkit": SB_TOOLKIT,
            "toolkit_host": str(toolkit_dir()),
        },
        **({"note": note_text} if note_text else {}),
    }
    return info


# Crash-classified failures (issue #27): the VM dies silently mid-session
# (0x80072746 / WSAECONNRESET on the host's RDP/HCS channel) and the next
# command must get a specific, actionable error — not a generic "dead".
_CRASH_CLOSED = ("error", "the sandbox VM crashed mid-session (0x80072746-class: "
                 "the host-sandbox connection was forcibly closed). VM state is "
                 "lost; toolkit and workspace writes persist. A fresh sandbox "
                 "boot was attempted — call sandbox_test again if it did not "
                 "come up.")
_CRASH_DEAD = ("error", "no acknowledgement from the sandbox runner and the VM "
               "process is gone — the sandbox crashed mid-session (0x80072746-"
               "class). A fresh sandbox boot was attempted — call sandbox_test "
               "again if it did not come up.")


def _proc_died_during_run() -> bool:
    """The Popen we own exited while a command was outstanding. An adopted
    session (no Popen) cannot be checked this way — PID liveness there is
    too expensive per poll."""
    return bool(_SESSION and _SESSION.get("proc") is not None
                and _SESSION["proc"].poll() is not None)


def _session_vm_gone(session: dict) -> bool:
    """True when the session's VM process has vanished (crash class)."""
    if not session:
        return False
    proc = session.get("proc")
    if proc is not None:
        return proc.poll() is not None
    pid = session.get("pid")
    return bool(pid) and pid not in _sandbox_pids()


def _crash_result(logs: Path | None, message: str) -> dict:
    """Crash-classified error with a best-effort diagnostics snapshot."""
    result: dict = {"error": message, "crashed": True}
    if logs is not None:
        diag = _snapshot_crash_diagnostics(logs)
        if diag:
            result["crash_diagnostics"] = diag
    return result


def _busy_wait_seconds() -> float:
    """How long a second caller waits for the sandbox before giving up with
    a busy error (sandbox.busy_wait_seconds; clamped 0-120)."""
    raw = _cfg().get("busy_wait_seconds", BUSY_WAIT_DEFAULT)
    try:
        return max(0, min(120, float(raw)))
    except (TypeError, ValueError):
        return float(BUSY_WAIT_DEFAULT)


_BUSY_ERROR = (
    "the sandbox is single-instance and busy with a command from another "
    "conversation (waited {wait:.0f}s). Do NOT call sandbox_test — retry "
    "sandbox_run in a little while, or continue with work that does not "
    "need the sandbox."
)


def _run_command(command: str, timeout: int) -> dict:
    """One command round-trip; no reboot logic (run_sync wraps it).

    The whole round-trip runs under the sandbox mutex (issue #27): the VM
    is single-instance and its answer channel is shared (one output.txt,
    one runtime.json), so concurrent commands from multiple chats would
    read each other's output. A second caller waits up to
    sandbox.busy_wait_seconds, then gets a busy error instead of silently
    interleaving."""
    global _SESSION
    if not _alive():
        session = _SESSION
        _SESSION = None
        if session and _session_vm_gone(session):
            return _crash_result(session.get("logs"), _CRASH_CLOSED[1])
        return {"error": "no sandbox session; call sandbox_test first"}
    logs: Path = _SESSION["logs"]
    wait = _busy_wait_seconds()
    if not _lock.acquire(timeout=wait):
        return {"error": _BUSY_ERROR.format(wait=wait), "busy": True}
    try:
        # The session may have ended (crash, stop) while we waited.
        if not _alive():
            session = _SESSION
            _SESSION = None
            if session and _session_vm_gone(session):
                return _crash_result(session.get("logs"), _CRASH_CLOSED[1])
            return {"error": "no sandbox session; call sandbox_test first"}
        n = _next_seq(logs)
        cmd_file = logs / f"cmd.{n}.ps1"
        done_file = logs / f"done.{n}"
        res_file = logs / f"res.{n}.json"
        (logs / "runtime.json").write_text(
            json.dumps({"command_timeout_seconds": timeout}), encoding="utf-8")
        cmd_file.write_text(command, encoding="utf-8")

        # The sandbox enforces the deadline itself and still reports partial
        # output; the host waits out that timeout plus a grace window before
        # declaring the channel dead.
        deadline = time.time() + timeout + 30
        while time.time() < deadline:
            if done_file.exists():
                break
            if _proc_died_during_run():
                _SESSION = None
                return _crash_result(logs, _CRASH_CLOSED[1])
            time.sleep(POLL_INTERVAL)
        else:
            # Timeout with no ack. Classify: a VM still in the process list is
            # a hung command channel; a vanished VM is the crash class. Either
            # way the session is over — but only the crash gets diagnostics.
            crashed = _session_vm_gone(_SESSION or {})
            _SESSION = None
            if crashed:
                return _crash_result(logs, _CRASH_DEAD[1])
            return {"error": "no acknowledgement from the sandbox runner; the "
                             "session looks dead — call sandbox_test to start "
                             "a fresh sandbox"}
    finally:
        _lock.release()

    meta: dict = {}
    try:
        # utf-8-sig strips a BOM if one is present (the meta file is
        # written ASCII today, but don't bet the parse on it).
        meta = json.loads(res_file.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        meta = {"exit_code": None, "timed_out": False}
    try:
        output = (logs / "output.txt").read_text(
            encoding="utf-8-sig", errors="replace")
    except OSError:
        output = ""
    truncated = False
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS]
        truncated = True
    return {
        "exit_code": meta.get("exit_code"),
        "timed_out": bool(meta.get("timed_out")),
        "output": output,
        **({"truncated": True} if truncated else {}),
    }


def run_sync(command: str, timeout: int) -> dict:
    """Execute one PowerShell command inside the live sandbox. Blocking.

    On a detected mid-session VM death (issue #27 crash class), snapshot
    crash diagnostics and — once — transparently reboot via start_sync
    (state loss is acceptable for a disposable VM) instead of leaving the
    model with a dead session and a generic error."""
    global _SESSION
    result = _run_command(command, timeout)
    if not result.get("crashed"):
        return result
    diag = result.pop("crash_diagnostics", None)
    if diag:
        result["crash_diagnostics"] = diag
    if not _cfg().get("auto_reboot_on_crash", True):
        return result
    workspace = None
    # _run_command cleared _SESSION on every crash path; recover the
    # workspace root from the session dir naming is unreliable, so the
    # reboot uses the workspace captured at start time — saved on the
    # session before the crash cleared it, mirrored here.
    workspace = _LAST_WORKSPACE
    if not workspace:
        return result
    reboot = start_sync(workspace)
    if "error" in reboot:
        result["reboot"] = f"automatic reboot failed: {reboot['error']}"
    else:
        result["reboot"] = "sandbox rebooted automatically; run the " \
                           "command again (in-VM state was lost)"
    return result


def status_sync() -> dict:
    cfg = _cfg()
    exe = sandbox_exe()
    running = _alive()
    return {
        "available": exe is not None,
        "enabled": bool(cfg.get("enabled", True)),
        "running": running,
        **({"sandbox_pid": _SESSION.get("pid")} if running else {}),
        "toolkit_host": str(toolkit_dir()),
        **({"workspace_mount": SB_WS} if running else {}),
    }


def stop_sync() -> dict:
    global _SESSION
    if not _alive():
        _stop_preview()
        _SESSION = None
        return {"stopped": False, "note": "no live sandbox session"}
    # Stopping mid-command would strand another chat's waiter on a session
    # that vanishes under it; take the mutex (bounded) so stop lands
    # between commands. A long wait means a command is mid-flight.
    wait = _busy_wait_seconds()
    if not _lock.acquire(timeout=wait):
        return {"stopped": False,
                "note": "the sandbox is busy with a command from another "
                        "conversation; try sandbox_stop again in a moment"}
    try:
        if not _alive():
            _stop_preview()
            _SESSION = None
            return {"stopped": False, "note": "no live sandbox session"}
        proc = _SESSION.get("proc")
        pid = _SESSION.get("pid")
        try:
            if proc is not None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, creationflags=_CREATE_NO_WINDOW,
                        timeout=15,
                    )
                else:
                    proc.terminate()
            elif pid:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, creationflags=_CREATE_NO_WINDOW,
                    timeout=15,
                )
        except Exception as e:  # noqa: BLE001 — PID may already be gone
            _stop_preview()
            _SESSION = None
            return {"stopped": False, "note": f"stop attempt failed: {e}"}
        _stop_preview()
        _SESSION = None
        return {"stopped": True, "note": "sandbox disposed; toolkit and "
                                         "workspace writes persist on the host"}
    finally:
        _lock.release()


# ---------------------------------------------------------------- executors

async def sandbox_test(workspace: str, timeout_seconds: int = None) -> dict:
    """Start or reuse the sandbox (model entry point)."""
    if timeout_seconds is not None:
        try:
            int(timeout_seconds)
        except (TypeError, ValueError):
            return {"error": "timeout_seconds must be an integer"}
    return await asyncio.to_thread(start_sync, workspace)


def _missing_command_hint(output: str) -> str | None:
    """Nudge for the guaranteed first-contact failure: the VM is a clean
    Windows image, so the first git/python/node call throws
    CommandNotFoundException — and, because PowerShell exit codes only
    reflect native commands, it arrives with exit_code 0. Fires on the
    output text, never the exit code."""
    low = (output or "").lower()
    if ("is not recognized as the name of a cmdlet" not in low
            and "commandnotfoundexception" not in low):
        return None
    return (
        "a command is missing inside the VM: it is a clean Windows "
        "image and host tools do not propagate. Install it into the "
        "toolkit, which persists to the host and is inherited by every "
        "future sandbox. On PATH inside the VM: toolkit, toolkit\\bin, "
        "toolkit\\Scripts, toolkit\\node_modules\\.bin. For a zipped "
        "tool: expand into toolkit\\<name>, then shim its exe from "
        "toolkit\\bin\\<name>.cmd when the archive has no bin-layout "
        "entry point."
    )


def _mcp_hint(output: str) -> str | None:
    """Nudge for in-VM GUI automation: windows-mcp is the primary GUI
    layer. Fires when output shows AHK muscle memory (ControlSend/
    ControlClick/AutoHotkey) or an MCP connection failure, pointing the
    session at the vendored windows-mcp server instead. Fires on the
    output text, never the exit code."""
    low = (output or "").lower()
    ahk = ("controlsend" in low or "controlclick" in low
           or "autohotkey" in low)
    down = ("connection refused" in low or "could not connect" in low
            or "unable to connect" in low)
    if not ahk and not down:
        return None
    if down:
        return (
            "the windows-mcp GUI server may not be up yet (it "
            "auto-starts at boot but the first run pip-installs deps, "
            "~60s). Retry after a short wait, or start it manually: "
            "sandbox_run 'powershell -ExecutionPolicy Bypass -File "
            "C:\\Users\\WDAGUtilityAccount\\Desktop\\toolkit\\bin\\"
            "windows-mcp-serve.ps1' and read the URL it prints. Connect "
            "with NO trailing slash on /mcp and Bearer auth."
        )
    return (
        "AutoHotkey is retired: the windows-mcp MCP server is the "
        "primary in-VM GUI layer (it auto-starts at boot; connection "
        "info in the mapped logs dir as mcp.json). Drive the GUI via "
        "its MCP tools over HTTP from the host: Snapshot first (UIA "
        "tree with labeled elements), then Click/Type with the label "
        "or loc:[x,y] it returns \u2014 no coordinate guessing, no "
        "background-input tricks."
    )


def _dialog_stall_hint(output: str, timed_out: bool) -> str | None:
    """Nudge for the installer-dialog stall: an interactive installer run
    unattended blocks on a permission dialog until the command times out
    with no output. Fires on the timeout flag + empty output."""
    if not timed_out or (output or "").strip():
        return None
    return (
        "the command timed out with no output — likely an interactive "
        "installer or command waiting on a dialog. Kill it (taskkill "
        "/IM <name> /F), then redo it SILENTLY: python installer "
        "'/quiet InstallAllUsers=1 PrependPath=1', 'msiexec /qn', "
        "'winget install --silent', or use a zip/portable distribution "
        "with no installer. Never run interactive installers unattended. "
        "If a dialog is unavoidable, drive it via the windows-mcp "
        "server's Click tool (find the button via Snapshot) instead "
        "of babysitting."
    )


async def sandbox_run(workspace: str, command: str,
                      timeout_seconds: int = 120) -> dict:
    """Run one PowerShell command inside the live sandbox."""
    try:
        timeout = max(1, min(int(timeout_seconds or 120), MAX_RUN_TIMEOUT))
    except (TypeError, ValueError):
        return {"error": "timeout_seconds must be an integer"}
    if not str(command or "").strip():
        return {"error": "command must be a non-empty PowerShell command"}
    result = await asyncio.to_thread(run_sync, str(command), timeout)
    if "error" not in result and result.get("exit_code") == 0:
        result.setdefault(
            "note",
            "batch commands where you can: each sandbox round-trip costs "
            "~1-3s of file polling",
        )
    hint = _missing_command_hint(str(result.get("output") or ""))
    if hint:
        result["hint"] = hint
    mcp = _mcp_hint(str(result.get("output") or ""))
    if mcp:
        result["hint"] = f"{result.get('hint', '')} {mcp}".strip()
    stall = _dialog_stall_hint(str(result.get("output") or ""),
                               bool(result.get("timed_out")))
    if stall:
        result["hint"] = f"{result.get('hint', '')} {stall}".strip()
    return result


async def sandbox_status(workspace: str) -> dict:
    return status_sync()


async def sandbox_stop(workspace: str) -> dict:
    return await asyncio.to_thread(stop_sync)


# ---------------------------------------------------------------- prompt

def prompt_section() -> str:
    return (
        "# Windows Sandbox (the default place to run things)\n\n"
        "- Run builds, full test suites, installs, servers, and GUI tests "
        "INSIDE the sandbox: boot it with `sandbox_test`, then `sandbox_run` "
        "to execute PowerShell commands inside it. Host bash/powershell are "
        "for file ops, git, non-executing checks, and read-only smoke checks "
        "that stay within the workspace (a focused test, typecheck, lint, or "
        "import). `sandbox_status` reports state; "
        "`sandbox_stop` disposes it.\n"
        "- The sandbox maps the workspace at C:\\Users\\WDAGUtilityAccount"
        "\\Desktop\\ws (your sandbox cwd) and the persistent dev toolkit "
        "at ...\\Desktop\\toolkit (on PATH). Host execution is allowed "
        "for read-only smoke checks that stay within the workspace (a focused "
        "test, typecheck, lint, or import); builds, full suites, installs, "
        "servers, and GUI tests belong in the sandbox. The toolkit is mounted "
        "read/write and persists on the host: tools installed inside any "
        "sandbox (into the toolkit) are inherited by every future "
        "sandbox, so add missing dev tools there instead of skipping a "
        "verification step.\n"
        "- Keep the work INSIDE the VM: if the app under test needs a "
        "browser, runtime or portable tool, install/download it into "
        "the sandbox (toolkit) and run it there — never launch a host "
        "equivalent (host browser, host python) to exercise the app. "
        "And NEVER drive the sandbox's GUI with the host mouse/keyboard "
        "tools (mouse_click, type_text, press_key, ...): they move the "
        "user's REAL desktop input. The VM has its own input session — "
        "drive its GUI via the windows-mcp MCP server (below).\n"
        "- The VM is a CLEAN WINDOWS IMAGE: git, python, node and other "
        "dev tools are NOT preinstalled — expect 'is not recognized as "
        "the name of a cmdlet' on first use. BEFORE downloading anything, "
        "read toolkit\\state.json (one round-trip): it lists what the "
        "toolkit already contains and how to check each tool. Install what "
        "you need into the toolkit (see above) rather than concluding the "
        "task cannot be verified, and add what you installed to "
        "state.json so future sessions skip the re-download.\n"
        "- Something look wedged (a command produced nothing / timed out "
        "with no output)? The VM can screenshot ITSELF: run "
        "toolkit\\bin\\vm-capture.ps1 via sandbox_run and view_image the "
        "PNG it writes (host side: ~/.yaah/toolkit/vm-screen.png). A modal "
        "dialog left by a failed installer run is invisible to "
        "sandbox_run output; vm-capture makes it visible in one "
        "round-trip, then kill the offending window/process by title.\n"
        "- NEVER run interactive installers unattended — they stall the "
        "session on a permission dialog. Use silent flags: python "
        "installer '/quiet InstallAllUsers=1 PrependPath=1', 'msiexec "
        "/qn', 'winget install --silent', or prefer zip/portable "
        "distributions (no installer at all). If a command times out "
        "with no output, assume a dialog stall: kill the process "
        "(taskkill /IM <name> /F) and redo it silently. If a dialog is "
        "truly unavoidable, drive it via the windows-mcp server's "
        "Click tool (find the button via Snapshot) instead of "
        "babysitting the screen.\n"
        "- Toolkit dirs prepended to PATH inside the VM: toolkit, "
        "toolkit\\bin, toolkit\\Scripts, toolkit\\node_modules\\.bin. For "
        "a zipped tool: expand into toolkit\\<name>, then drop a .cmd "
        "shim into toolkit\\bin when the archive has no bin-layout exe "
        "(e.g. MinGit ships cmd\\git.exe, so bin\\git.cmd calls "
        "..\\mingit\\cmd\\git.exe).\n"
        "- git against the mapped workspace fails with 'dubious "
        "ownership' (host SID vs VM user). Fix it once, permanently: "
        "write toolkit\\gitconfig containing '[safe]' + 'directory = *' "
        "and point GIT_CONFIG_GLOBAL at it (a toolkit\\bin\\git.cmd shim "
        "can set the variable before invoking the real git.exe).\n"
        "- git must never open its interactive editor in the VM (it "
        "hangs the command channel until the timeout): pass -m "
        "'<message>' to git commit and set GIT_EDITOR=true for "
        "editor-requiring commands (rebase --continue, tag -a, "
        "commit --amend). The bootstrap points GIT_EDITOR/EDITOR/"
        "VISUAL at a no-op, so an editor-less commit fails fast with "
        "'empty message' instead of hanging.\n"
        "- GUI automation inside the VM: the windows-mcp MCP server is the "
        "PRIMARY GUI layer. It AUTO-STARTS at sandbox boot (the bootstrap "
        "runs toolkit\\bin\\windows-mcp-serve.ps1 detached; first boot "
        "may take ~60s extra while it pip-installs pinned deps). Its "
        "connection info is written to the mapped logs dir as mcp.json "
        "(host side: <session logs dir>\\mcp.json — the logs dir is "
        "the one sandbox_status / the session files show). It gives "
        "{\"url\": \"http://<vm-ip>:8000/mcp\", \"auth\": \"Bearer "
        "sandbox-demo-key\"}. Connect EXACTLY like this (raw HTTP from "
        "the host — NO trailing slash on /mcp; a trailing slash "
        "307-redirects and silently DROPS the POST body):\n"
        "  1) POST <url> with headers {\"Authorization\": \"Bearer "
        "sandbox-demo-key\", \"Content-Type\": \"application/json\", "
        "\"Accept\": \"application/json, text/event-stream\"} and body "
        "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":"
        "{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},"
        "\"clientInfo\":{\"name\":\"yaah\",\"version\":\"1.0\"}}} — grab "
        "the mcp-session-id RESPONSE header.\n"
        "  2) POST notifications/initialized (same headers + the "
        "mcp-session-id header).\n"
        "  3) tools/list, then tools/call — ALWAYS sending the "
        "mcp-session-id header.\n"
        "  Key tools (input schemas are the truth; descriptions lie): "
        "Snapshot returns the UIA tree with labeled elements — call it "
        "FIRST, then Click/Type with the label or loc:[x,y] it gave you "
        "(no coordinate guessing). App launches apps: use "
        "mode=launch_executable + a full executable path (Edge is NOT in "
        "the VM's start menu index; e.g. C:\\Program Files (x86)\\"
        "Microsoft\\Edge\\Application\\msedge.exe), NOT 'action'. "
        "Shortcut takes 'shortcut', NOT 'keys'. The PowerShell tool runs "
        "commands in the VM. If the server is not up (connection "
        "refused), start it manually: sandbox_run \"powershell "
        "-ExecutionPolicy Bypass -File "
        "C:\\Users\\WDAGUtilityAccount\\Desktop\\toolkit\\bin\\"
        "windows-mcp-serve.ps1\" and read the URL it prints.\n"
        "- Dispose the sandbox when the work is done: call "
        "`sandbox_stop` after smoke tests, reproductions or testing "
        "wrap up — the VM is an 8 GB window on the user's desktop, not "
        "something to leave open.\n"
        "- If Windows Sandbox is not enabled in Windows, sandbox_test "
        "returns enablement instructions — relay them to the user.\n"
        "- The VM's Defender firewall is disabled at boot by the "
        "bootstrap (the VM is disposable and behind the host's NAT), so "
        "listening ports never trigger the Windows Security Alert "
        "dialog.\n"
        "- Each sandbox_run round-trip costs ~1-3s of file polling: batch "
        "commands. The access-mode gate still applies: sandbox_test "
        "prompts in ask mode and sandbox_run is gated like bash."
    )


SANDBOX_EXECUTORS = {
    "sandbox_test": sandbox_test,
    "sandbox_run": sandbox_run,
    "sandbox_status": sandbox_status,
    "sandbox_stop": sandbox_stop,
}

SANDBOX_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "sandbox_test",
            "description": (
                "Start (or reuse) a disposable Windows Sandbox VM — the "
                "default place to run the workspace's app, tests and "
                "builds (host bash/powershell are for file ops, git and "
                "non-executing checks). All work for the app under test "
                "stays inside the VM: its dependencies run in here (never "
                "host equivalents), and its GUI is driven via the "
                "windows-mcp MCP server (auto-started at boot; see the "
                "sandbox prompt section) — never the host mouse/keyboard tools. "
                "Prompts the user in ask mode. "
                "Returns once the sandbox is ready (first boot is a slow "
                "cold start)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            "Max seconds to wait for sandbox boot "
                            "(default: config, 180s)"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_run",
            "description": (
                "Execute one PowerShell command INSIDE the running sandbox "
                "(call sandbox_test first). The workspace is mounted at "
                "C:\\Users\\WDAGUtilityAccount\\Desktop\\ws (the sandbox "
                "cwd) and the persistent dev toolkit at ...\\Desktop\\"
                "toolkit (on PATH; installs there persist to the host and "
                "every future sandbox). The VM is a CLEAN WINDOWS IMAGE: "
                "check toolkit\\state.json FIRST - it lists the tools "
                "already present (plus bundled helpers like "
                "toolkit\\bin\\vm-capture.ps1, which screenshots the VM "
                "itself when a command wedges), "
                "git/python/node are not preinstalled — on 'is not "
                "recognized as the name of a cmdlet', install the tool "
                "into the toolkit (PATH inside the VM: toolkit, "
                "toolkit\\bin, toolkit\\Scripts, toolkit\\node_modules"
                "\\.bin; shim zipped tools' exe from toolkit\\bin\\<name>"
                ".cmd). For GUI automation in the VM use the windows-mcp MCP "
                "server (auto-started at boot; connection info in the "
                "mapped logs dir as mcp.json; connect over HTTP with "
                "Bearer auth, NO trailing slash on /mcp): Snapshot "
                "first, then Click/Type with the labeled element. "
                "File-polling transport: each "
                "command costs ~1-3s — batch work into fewer commands."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Default 120, max 900",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_status",
            "description": (
                "Report sandbox availability (is the Windows feature "
                "enabled), whether a session is running, and the mounted "
                "paths."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_stop",
            "description": (
                "Dispose the running sandbox when the work is done "
                "(after smoke tests, reproductions, testing) — do not "
                "leave the VM open on the user's desktop. Everything "
                "written to the workspace or toolkit mounts already "
                "persists on the host."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
