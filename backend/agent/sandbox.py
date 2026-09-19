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
import os
import re
import subprocess
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
    "vgpu": "Default",
    "map_workspace": True,
    "startup_timeout": 180,
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
        f"  <vGPU>{cfg.get('vgpu', 'Default')}</vGPU>\n"
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


def _session_info(note: str | None = None) -> dict:
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


def run_sync(command: str, timeout: int) -> dict:
    """Execute one PowerShell command inside the live sandbox. Blocking."""
    global _SESSION
    if not _alive():
        if _SESSION:
            _SESSION = None
        return {"error": "no sandbox session; call sandbox_test first"}
    logs: Path = _SESSION["logs"]
    with _lock:
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
        if _SESSION and _SESSION.get("proc") is not None \
                and _SESSION["proc"].poll() is not None:
            _SESSION = None
            return {"error": "the sandbox was closed while the command ran"}
        time.sleep(POLL_INTERVAL)
    else:
        _SESSION = None
        return {"error": "no acknowledgement from the sandbox runner; the "
                         "session looks dead — call sandbox_test to start "
                         "a fresh sandbox"}

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
        _SESSION = None
        return {"stopped": False, "note": f"stop attempt failed: {e}"}
    _SESSION = None
    return {"stopped": True, "note": "sandbox disposed; toolkit and "
                                     "workspace writes persist on the host"}


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


def _ahk_hint(output: str) -> str | None:
    """Nudge for in-VM GUI automation failures: AutoHotkey v2 is the
    in-VM input layer (its input never touches the host), and the
    background-input techniques have sharp edges that fail silently.
    Fires on the output text, never the exit code."""
    low = (output or "").lower()
    if ("controlsend" not in low and "controlclick" not in low
            and "autohotkey" not in low):
        return None
    return (
        "AutoHotkey v2 is the in-VM GUI input layer (its input never "
        "touches the host). Run scripts via Start-Process -Wait with "
        "/ErrorStdOut — without it, script errors become modal dialogs "
        "that hang the session. Background input needs explicit targets: "
        "ControlClick 'x300 y200', hwnd, , 'Left', 1, 'NA' clicks a "
        "background window without activating it; ControlSend keys, "
        "'Edit1', hwnd types into a background window (window-level "
        "ControlSend without a control target silently does nothing); "
        "ControlGetText reads state. Never name a variable after an AHK "
        "function (log, WinGetList). UIA-v2 "
        "(github.com/Descolada/UIA-v2) adds UI Automation element "
        "discovery + pattern actions to AHK."
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
    ahk = _ahk_hint(str(result.get("output") or ""))
    if ahk:
        result["hint"] = f"{result.get('hint', '')} {ahk}".strip()
    return result


async def sandbox_status(workspace: str) -> dict:
    return status_sync()


async def sandbox_stop(workspace: str) -> dict:
    return await asyncio.to_thread(stop_sync)


# ---------------------------------------------------------------- prompt

def prompt_section() -> str:
    return (
        "# Windows Sandbox (the default place to run things)\n\n"
        "- Run the workspace's app, tests and builds INSIDE the sandbox: "
        "boot it with `sandbox_test`, then `sandbox_run` to execute "
        "PowerShell commands inside it. Host bash/powershell are for "
        "file ops, git, and non-executing checks — not for running the "
        "workspace's code. `sandbox_status` reports state; "
        "`sandbox_stop` disposes it.\n"
        "- The sandbox maps the workspace at C:\\Users\\WDAGUtilityAccount"
        "\\Desktop\\ws (your sandbox cwd) and the persistent dev toolkit "
        "at ...\\Desktop\\toolkit (on PATH). The toolkit is mounted "
        "read/write and persists on the host: tools installed inside any "
        "sandbox (into the toolkit) are inherited by every future "
        "sandbox, so add missing dev tools there instead of skipping a "
        "verification step.\n"
        "- The VM is a CLEAN WINDOWS IMAGE: git, python, node and other "
        "dev tools are NOT preinstalled — expect 'is not recognized as "
        "the name of a cmdlet' on first use. Install what you need into "
        "the toolkit (see above) rather than concluding the task cannot "
        "be verified.\n"
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
        "- GUI automation inside the VM: AutoHotkey v2 is the in-VM "
        "input layer — its input never touches the host (the VM has its "
        "own input session; the host user is unaffected). Install once: "
        "download the AutoHotkey zip from "
        "https://www.autohotkey.com/download/2.0/ and expand to "
        "toolkit\\ahk (persists to the host + future sandboxes). Run "
        "scripts via Start-Process -Wait with /ErrorStdOut — WITHOUT "
        "/ErrorStdOut, script errors become modal dialogs that hang the "
        "session. Techniques (verified): ControlClick 'x300 y200', hwnd, "
        ", 'Left', 1, 'NA' clicks a BACKGROUND window without activating "
        "it; ControlSend keys, 'Edit1', hwnd types into a background "
        "window but REQUIRES an explicit control target (window-level "
        "ControlSend silently does nothing); ControlGetText reads state. "
        "UIA-v2 (github.com/Descolada/UIA-v2) adds full UI Automation to "
        "AHK for element discovery + pattern actions. AHK v2 traps: "
        "never name a variable after a function (log, WinGetList fail "
        "with 'This Func cannot be used as an output variable'); "
        "ControlSend's signature is (Keys, Control, WinTitle).\n"
        "- If Windows Sandbox is not enabled in Windows, sandbox_test "
        "returns enablement instructions — relay them to the user.\n"
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
                "non-executing checks). Prompts the user in ask mode. "
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
                "git/python/node are not preinstalled — on 'is not "
                "recognized as the name of a cmdlet', install the tool "
                "into the toolkit (PATH inside the VM: toolkit, "
                "toolkit\\bin, toolkit\\Scripts, toolkit\\node_modules"
                "\\.bin; shim zipped tools' exe from toolkit\\bin\\<name>"
                ".cmd). For GUI automation in the VM use AutoHotkey v2 "
                "(in-VM input never touches the host): run via "
                "Start-Process -Wait with /ErrorStdOut, and target "
                "background windows with ControlClick 'NA' / ControlSend "
                "with an explicit control. File-polling transport: each "
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
                "Dispose the running sandbox. Everything written to the "
                "workspace or toolkit mounts already persists on the host."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
