"""YAAH server executable (``yaah-server-setup`` on Windows, ``yaah-server``
on Linux) — one self-contained binary.

Serves the same FastAPI app the desktop sidecar runs (backend.main:app),
packaged for machines without Python or a GUI. A YAAH desktop instance
connects to it over the LAN exactly as it would connect to another
desktop app — the host role (handshake, workspace-tool exec, passphrase
gate, mDNS beacon) is already implemented in backend.main.

The host is stateless by design: conversations, provider keys and the
agent loop stay on the client. The server only executes workspace tools
(shell, files, git) in its own workspace (its home directory), so this
binary needs none of the GUI-only weight (voice, Windows-only computer
use) the desktop sidecar bundles.

Usage (the SAME exe does everything on Windows — service host, service
management, and the GUI setup wizard):

    yaah-server-setup setup
        GUI installer: a Tkinter window collects the passphrase + display
        name (+ port/bind), pre-fills an existing install's settings,
        copies itself to %LOCALAPPDATA%\\YAAH\\, registers the Windows
        service (automatic startup, running as LocalSystem - no account
        password; the installing user's profile is recorded in the
        service's Parameters so the workspace is their real home), and
        starts it. Needs an elevated prompt (UAC consent for admins, a
        credential prompt for standard users).
        This is also what happens when you double-click the exe.
    yaah-server-setup uninstall
        GUI uninstaller (also what Programs & Features runs): asks what
        to remove (service, data folder, program file), shows the
        summary, then removes it.
    yaah-server-setup status / logs
        Small status window / opens the logs folder in Explorer.
    yaah-server-setup install|remove|start|stop|restart
        Service management, unchanged (elevated prompt; for scripts and
        admins - the GUI wraps, never replaces, these).
    yaah-server-setup install|remove|start|stop|restart
        Service management (elevated prompt).
    yaah-server-setup serve [--host H] [--port P] [--passphrase SECRET]
                [--display-name NAME] [--no-hosting]
        Foreground console run.
    (bare ``yaah-server-setup`` with no arguments is the SCM service
    entry point on Windows; on Linux it serves in the foreground — that
    is what the systemd unit runs.)

On Linux the binary is ``/usr/bin/yaah-server`` inside the .deb (unit
``yaah-server@<user>``, passphrase via /etc/yaah/yaah.conf). The GUI
installer, the uninstaller and the ARP registration are Windows-only.

--passphrase / --display-name persist to the same ~/.yaah/config.json the
desktop app's Settings writes, so they survive restarts and the desktop
app sees the same values. Without a passphrase the server still starts
(and is discoverable), but every remote exec request is refused with 401
— the same rule the desktop app applies.
"""

import argparse
import os
import sys
import time
from queue import Empty
from queue import Queue


SERVICE_NAME = "YaahServer"
SERVICE_DISPLAY = "YAAH Headless Server"
SERVICE_DESC = "YAAH remote-exec host: serves workspace tools to YAAH desktop clients on the LAN."
WIN_INSTALL_DIR = "YAAH"
ARP_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\YaahServer"
START_MENU_DIR_NAME = "YAAH Server"
LOG_DIR_NAME = "logs"
PUBLISHER = "YAAH"

# The service runs as LocalSystem (CreateService's default - the wizard
# passes no --username/--password), whose Path.home() is
# System32\config\systemprofile. The wizard records the installing
# user's real profile under the service's Parameters key; SvcDoRun
# re-points the per-user paths there before any backend import resolves
# Path.home() (backend.agent.config captures CONFIG_PATH at import).
def _no_window() -> int:
    """CREATE_NO_WINDOW on Windows: netsh/sc are console apps, and a
    console parent spawning them flashes a console window - fatal in
    the --windowed GUI flow (issue #466: "no console window during GUI
    operation"). Captured/inherited stdio still flows; only the extra
    console is suppressed."""
    return 0x08000000 if sys.platform == "win32" else 0


PARAM_SUBKEY = r"SYSTEM\CurrentControlSet\Services\YaahServer"
PROFILE_DIR_VALUE = "ProfileDir"
PORT_VALUE = "Port"
HOST_VALUE = "Host"


def run_server(host: str, port: int) -> None:
    """Run the API server in the foreground (console or service context)."""
    os.environ.setdefault("YAAH_HEADLESS", "1")
    # The mDNS beacon (backend.main lifespan) advertises this port - LAN
    # clients dial what this process actually binds, not a constant.
    os.environ["YAAH_API_PORT"] = str(port)
    # The --windowed build has no stdio at all: uvicorn's loggers write
    # to sys.stdout/sys.stderr and crash on None. Same guard SvcDoRun
    # applies (CodeRabbit #466 round 2: `serve` in a windowed build).
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    import uvicorn

    from backend.agent.remote import PROTOCOL_VERSION
    from backend.main import app

    print(f"yaah-server listening on {host}:{port} (protocol {PROTOCOL_VERSION})")
    print("connect from YAAH desktop: host switcher (mDNS) or direct IP:port")
    uvicorn.run(app, host=host, port=port, log_level="info")


class InstallError(Exception):
    """A fatal install problem (GUI shows it verbatim; CLI prints it)."""


def _display_version() -> str:
    """The version string for ARP and the status window (the server ships
    the desktop backend, so its version is ours)."""
    try:
        from backend._version import __version__
        return __version__
    except Exception:  # noqa: BLE001 - a dev tree without _version.py
        return "0.0.0-dev"


def _configure_bind(host: str = "0.0.0.0", port: int = 8765) -> bool:
    """Record the wizard's bind + port for the service. True on success.

    The SCM service context reads these on every start (SvcDoRun binds
    them) and the mDNS beacon advertises the port LAN clients dial, so
    both are persisted even when they equal the defaults - otherwise a
    later re-install returning to 8765 would leave a stale custom port
    in place. A failed write (locked-down registry) keeps the previous
    values; the caller warns (CodeRabbit #466 round 2).
    """
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, PARAM_SUBKEY + r"\Parameters", 0,
            winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY,
        ) as key:
            winreg.SetValueEx(key, PORT_VALUE, 0, winreg.REG_DWORD, int(port))
            winreg.SetValueEx(key, HOST_VALUE, 0, winreg.REG_SZ, str(host))
        return True
    except (ImportError, OSError):
        return False


def _configured_port() -> int:
    """The recorded port (8765 when absent or out of range)."""
    try:
        import winreg

        with winreg.OpenKeyEx(
            winreg.HKEY_LOCAL_MACHINE, PARAM_SUBKEY + r"\Parameters", 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            port, _t = winreg.QueryValueEx(key, PORT_VALUE)
    except (ImportError, OSError, ValueError):
        return 8765
    try:
        port = int(port)
    except (TypeError, ValueError):
        return 8765
    return port if 1 <= port <= 65535 else 8765


def _configured_bind() -> tuple[str, int]:
    """(host, port) recorded in the service's Parameters key - what
    SvcDoRun actually binds. A loopback bind is for same-box testing:
    the mDNS beacon still advertises the LAN interface, so a
    loopback-bound server is discoverable but refuses LAN connects."""
    try:
        import winreg

        with winreg.OpenKeyEx(
            winreg.HKEY_LOCAL_MACHINE, PARAM_SUBKEY + r"\Parameters", 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            host, _t = winreg.QueryValueEx(key, HOST_VALUE)
    except (ImportError, OSError, ValueError):
        host = "0.0.0.0"
    host = str(host).strip() or "0.0.0.0"
    return host, _configured_port()


def _log_dir() -> str:
    r"""<install dir>\logs - the folder the service tees its stdout to
    and the Start Menu "Server Logs" shortcut opens.

    Resolved from the INSTALLED exe's directory, never the running
    process's LOCALAPPDATA: the service runs as LocalSystem, whose
    LOCALAPPDATA is systemprofile's - resolving per-user would make the
    service write one folder while the user's shortcut opens another
    (CodeRabbit #466 round 2). The install dir is fixed at install
    time, so both contexts land on the same folder.
    """
    return os.path.join(os.path.dirname(_installed_exe()), LOG_DIR_NAME)


def _open_logs() -> None:
    """Open the logs folder in Explorer (creating it if needed)."""
    import subprocess

    path = _log_dir()
    os.makedirs(path, exist_ok=True)
    if hasattr(os, "startfile"):
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


def _write_arp(installed: str, uninstall_cmd: str,
               emit=None) -> bool:
    """Write the Programs & Features entry (issue #466's core gap).

    The quoted UninstallString is what ARP runs (right-click ->
    Uninstall); DisplayVersion parses dotted-decimal - the rc suffix
    (package.json's "1.0.19-rc.24") is stripped or ARP mangles the sort.
    NoDisplay is NOT set: the whole point is visibility. Returns success
    so the caller can surface failures in the GUI status label too - a
    silently missing ARP entry is the exact gap this issue is about
    (CodeRabbit #466 round 2). A failed write leaves the CLI verb as
    the only removal path, so the warning says exactly that.
    """
    said = emit or print
    version = _display_version().split("-")[0]
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, ARP_KEY, 0,
            winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY,
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ,
                              SERVICE_DISPLAY)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ,
                              version)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ,
                              os.path.dirname(installed))
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ,
                              installed)
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                              uninstall_cmd)
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        return True
    except (ImportError, OSError) as exc:
        said(f"warning: could not write the Programs & Features entry "
              f"({exc}) - uninstall with `{installed} remove` "
              "from an elevated prompt")
        return False


def _read_arp() -> dict:
    """The ARP values this install wrote ({} when absent/unreadable)."""
    try:
        import winreg

        with winreg.OpenKeyEx(
            winreg.HKEY_LOCAL_MACHINE, ARP_KEY, 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            values = {}
            i = 0
            while True:
                try:
                    name, val, _t = winreg.EnumValue(key, i)
                except OSError:
                    break
                values[name] = val
                i += 1
            return values
    except (ImportError, OSError):
        return {}


def _delete_arp() -> bool:
    """Remove the Programs & Features entry. True when it is gone."""
    try:
        import winreg

        winreg.DeleteKeyEx(
            winreg.HKEY_LOCAL_MACHINE, ARP_KEY,
            winreg.KEY_WOW64_64KEY)
        return True
    except FileNotFoundError:
        return True
    except (ImportError, OSError):
        return False


def _start_menu_dir() -> str:
    """All Users Start Menu folder for the YAAH Server shortcuts.

    All Users (not the per-user tree) so the entry exists for every
    account; a failure to create falls back to the current user's.
    """
    import ctypes.wintypes

    path_buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
    CSIDL_COMMON_PROGRAMS = 0x17
    SHGFP_TYPE_CURRENT = 0
    ok = ctypes.windll.shell32.SHGetFolderPathW(
        None, CSIDL_COMMON_PROGRAMS, None, SHGFP_TYPE_CURRENT, path_buf)
    base = path_buf.value if ok == 0 else os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"),
        "Microsoft", "Windows", "Start Menu", "Programs")
    return os.path.join(base, START_MENU_DIR_NAME)


def _write_shortcut(path: str, target: str, arguments: str, desc: str) -> None:
    """One .lnk via the shell COM object (no new deps: pywin32 already
    ships pythoncom; a frozen GUI-less box never runs this)."""
    import pythoncom
    from win32com.shell import shell

    shell_link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None,
        pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLinkA)
    link = shell_link.QueryInterface(pythoncom.IID_IPersistFile)
    shell_link.SetPath(target)
    shell_link.SetArguments(arguments)
    shell_link.SetDescription(desc)
    link.Save(path, 0)


def _write_start_menu(installed: str, emit=None) -> None:
    """The three Start Menu shortcuts under All Users\\YAAH Server.

    Uninstall runs the GUI uninstaller (not the bare remove verb) so the
    user gets the question-by-question flow; the status window is what
    the issue calls "Open Server Status"; logs opens the folder.
    Best-effort per shortcut: a failure to write shortcuts must not fail
    the install (the service is already in place at that point), but
    every failure is reported through emit (CodeRabbit #466 round 2).
    Targets are BARE paths: IShellLink::SetPath stores the path as-is,
    and embedded quotes make a .lnk unlaunchable (CodeRabbit #466
    round 2; the ARGUMENTS stay separate via SetArguments).
    """
    said = emit or print
    try:
        import pythoncom  # noqa: F401 - CoInitialize happens implicitly in
        # the main thread of a python.exe; frozen builds bootstrap it in
        # _gui_dispatch.
    except ImportError:
        said("warning: shortcuts skipped (pythoncom missing)")
        return
    menu = _start_menu_dir()
    try:
        os.makedirs(menu, exist_ok=True)
    except OSError as exc:
        said(f"warning: could not create {menu} ({exc})")
        return
    entries = [
        ("Uninstall YAAH Server.lnk", installed, "uninstall",
         "Remove the YAAH headless server"),
        ("Open Server Status.lnk", installed, "status",
         "YAAH server status: running state, port, last start"),
        ("Server Logs.lnk", installed, "logs",
         "Open the YAAH server logs folder"),
    ]
    for name, target, verb, desc in entries:
        try:
            _write_shortcut(os.path.join(menu, name), target, verb, desc)
        except Exception as exc:  # noqa: BLE001 - one bad shortcut must not
            # abort the remaining two (or the install itself)
            said(f"warning: could not write shortcut {name} ({exc})")


def _delete_start_menu() -> None:
    """Remove the Start Menu folder with its shortcuts (best-effort)."""
    import shutil

    shutil.rmtree(_start_menu_dir(), ignore_errors=True)


def _service_started_at() -> str:
    """When the service process last started, or "" - best-effort (the
    status window shows a dash when this fails: sparse is honest).

    Keyed by the service's registered PID (QUERY via sc), not the exe
    name: pythonservice.exe hosts many services, so name filtering can
    match an unrelated process (CodeRabbit #466 round 2). Bounded
    timeout so a stuck PowerShell cannot hang the status window.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["sc", "query", SERVICE_NAME], capture_output=True, text=True,
            check=False, timeout=5, creationflags=_no_window()).stdout
        pid = ""
        for line in out.splitlines():
            if "PID" in line:
                pid = line.split(":", 1)[-1].strip()
                break
        if not pid:
            return ""
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Process -Id " + pid + ").StartTime.ToString()"],
            capture_output=True, text=True, check=False, timeout=10,
            creationflags=_no_window()).stdout.strip()
        return out
    except Exception:  # noqa: BLE001 - cosmetic field only
        return ""


def _await_stopped(installed: str, timeout: float = 15.0) -> bool:
    """Poll the SCM until the service leaves RUNNING (True) or time out.

    Used before the upgrade self-copy: the SCM stop is asynchronous and
    the exe image stays locked until the process is actually gone.
    """
    deadline = time.time() + timeout
    while True:
        state = _service_state()
        if not state or "RUNNING" not in state:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _tcp_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """True when something answers on (host, port) — the install's
    ground truth (uvicorn must be listening)."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _do_install(*, passphrase, display, host, port, profile, emit=None) -> str:
    """Shared install sequence: GUI wizard and CLI setup both call this.

    The GUI passes emit() to surface progress lines in its status label
    (thread-safe via its queue); the CLI emits by printing.
    Returns the SCM state after the start ("4  RUNNING" on success).
    Raises InstallError with a user-facing message on any fatal step.
    """
    import shutil
    import socket
    import subprocess

    said = emit or print
    installed = _installed_exe()
    me = os.path.abspath(sys.executable)

    if not passphrase:
        raise InstallError("a passphrase is required (without one the "
                           "server refuses all remote access)")

    # Identity anchor: the launching user's profile (captured BEFORE
    # elevation by the caller) gets the passphrase config and is recorded
    # for the service, so the workspace is their real home.
    if not _is_elevated():
        # Re-spawn elevated via ShellExecuteW, passing the pre-elevation
        # profile through for the elevated continuation.
        import ctypes

        arguments = f'setup "{profile}"'
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", me, arguments, None, 1)
        if result <= 32:
            raise InstallError("could not elevate for service installation "
                               "(UAC prompt declined?)")
        return "elevated"  # the elevated process continues from `setup`

    # Self-copy: the service must not depend on this exe staying wherever
    # the user happened to run it from (e.g. Downloads). On an upgrade
    # the installed image is RUNNING - Windows locks a running image, so
    # stop the service first and wait for the SCM to confirm (CodeRabbit
    # #466 round 2: copy2 over the live image fails with PermissionError).
    if os.path.normcase(me) != os.path.normcase(installed):
        was_running = bool(_service_state())
        if was_running:
            said("stopping the running service for the upgrade...")
            subprocess.run([installed, "stop"], check=False,
                           creationflags=_no_window())
            if _await_stopped(installed):
                said("service stopped")
            else:
                raise InstallError(
                    "the YaahServer service did not stop - it is "
                    "restarting or hung. Stop it in Services.msc and "
                    "re-run setup.")
        try:
            os.makedirs(os.path.dirname(installed), exist_ok=True)
            shutil.copy2(me, installed)
            said(f"installed server to {installed}")
        except OSError as exc:
            raise InstallError(
                f"could not copy the server to {installed} ({exc}) - "
                "close any program using it and re-run setup") from exc

    said("saving passphrase and settings to the YAAH config...")
    _persist_remote({"passphrase": passphrase}, profile)
    if display:
        _persist_remote({"display_name": display}, profile)
    # Always persist port + host: a re-install returning to defaults
    # must overwrite a previous install's custom values.
    _persist_remote({"port": port, "host": host}, profile)

    # Register + start the service as LocalSystem: no --username/
    # --password, so CreateService defaults the account to LocalSystem —
    # no account password asked, no credentials stored (nothing breaks
    # when a password changes), no SeServiceLogonRight grant needed
    # (LocalSystem has it implicitly). HandleCommandLine parses with
    # getopt, which stops at the first positional — ALL options must
    # come first and the verb is the LAST argument. (Verified against
    # pywin32: any other order prints usage and exits 1.)
    said("installing service as LocalSystem (automatic startup)...")
    rc = subprocess.run(
        [installed, "--startup", "auto", "install"],
        creationflags=_no_window()).returncode
    if rc != 0:
        raise InstallError(f"service install failed (exit {rc})")

    # The service runs as LocalSystem but reads its config from the
    # profile the passphrase was just written to — record that directory
    # in the service's Parameters key so every start re-points the
    # per-user paths there (_apply_service_profile_dir).
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, PARAM_SUBKEY + r"\Parameters", 0,
            winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY,
        ) as key:
            winreg.SetValueEx(key, PROFILE_DIR_VALUE, 0,
                              winreg.REG_SZ, profile)
        said(f"service profile dir recorded: {profile}")
    except (ImportError, OSError) as exc:
        said(f"warning: could not record the profile dir ({exc}) - "
             "the service will start with no passphrase; re-run setup")

    # Bind + port choice (issue #466): the service must bind and
    # advertise what the wizard chose. A failed write is non-fatal but
    # MUST be visible: the service would run on stale defaults while
    # the wizard claimed otherwise (CodeRabbit #466 round 2).
    if _configure_bind(host, port):
        said(f"service bind recorded: {host}:{port}")
    else:
        said(f"warning: could not record the bind/port ({host}:{port}) - "
             "the service keeps its previous values; re-run setup from "
             "an elevated prompt")

    # Auto-restart on crash (every failure, 5s apart, counters reset daily).
    # Best-effort: arg spacing varies across sc builds.
    subprocess.run(
        ["sc", "failure", SERVICE_NAME, "reset=", "86400",
         "actions=", "restart/5000/restart/5000/restart/5000"],
        capture_output=True, creationflags=_no_window(),
    )

    # HandleCommandLine always exits 0 — its return code does not reflect
    # whether the service actually started. Poll the SCM for the truth.
    state = _start_and_await(installed)
    if not state.endswith("RUNNING"):
        raise InstallError(
            "service did not reach RUNNING (state: "
            f"{state or 'not installed'}) - check Services.msc > "
            f"{SERVICE_DISPLAY}. The Application log line "
            "'YaahServer crashed' names the cause; a port collision means "
            "a YAAH desktop app is running on this box.")

    # Windows Firewall inbound rules so remote clients reach us without
    # a manual netsh command (issue #465). The TCP rule opens the port
    # the wizard chose - not a hardcoded 8765 (CodeRabbit #466 round 2).
    _configure_firewall(installed, port=port)

    # Visibility (issue #466): Programs & Features entry + Start Menu.
    # The ARP UninstallString points at the GUI uninstaller with the
    # installing profile passed through (quoted paths throughout).
    _write_arp(installed, f'"{installed}" uninstall "{profile}"',
               emit=said)
    _write_start_menu(installed, emit=said)

    # Ground truth over claims: uvicorn must actually be listening - a
    # bounded poll (the SCM already reported RUNNING, so this settles in
    # seconds or not at all).
    for _ in range(5):
        if _tcp_port_open("127.0.0.1", port):
            said(f"server is listening on {host}:{port}")
            break
        time.sleep(1.0)
    else:
        said(f"warning: nothing answers on 127.0.0.1:{port} yet - give it "
             "a moment, then check the status window (a wrong bind or a "
             "firewall block shows up here)")
    return state


def _do_uninstall(*, profile, remove_service=True, remove_config=False,
                  remove_exe=False, emit=None) -> tuple[list, list]:
    """Shared removal sequence (GUI uninstaller's phases; `remove` adds
    the ARP/Start-Menu cleanup to the plain service verb).

    Returns (removed, failed) lists for the completion report. Never
    raises for a best-effort step that partially failed - the report
    says what survived.
    """
    import shutil
    import subprocess

    said = emit or print
    removed: list = []
    failed: list = []
    installed = _installed_exe()

    if remove_service:
        said("stopping and removing the service...")
        subprocess.run([installed, "remove"], check=False,
                       creationflags=_no_window())
        _configure_firewall(installed, remove=True)
        if _delete_arp():
            removed.append("Programs & Features entry")
        else:
            failed.append("Programs & Features entry (delete the registry "
                          "key HKLM\\" + ARP_KEY + " by hand)")
        _delete_start_menu()
        removed.append("Start Menu shortcuts")
        if _service_state():
            failed.append("YaahServer service (still present - check "
                          "Services.msc)")
        else:
            removed.append("YaahServer service")

    if remove_config:
        said("deleting the data folder...")
        data_dir = os.path.join(os.path.normpath(profile), ".yaah")
        try:
            shutil.rmtree(data_dir)
            removed.append(f"data folder {data_dir}")
        except OSError as exc:
            failed.append(f"data folder {data_dir} ({exc})")

    if remove_exe:
        said("removing the program file...")
        me = os.path.abspath(sys.executable)
        if os.path.normcase(installed) == os.path.normcase(me):
            # Windows cannot delete a running image: spawn a detached
            # deleter that removes it right after this process exits.
            # Reported as PENDING, not done - the deleter can fail (file
            # re-locked), so claiming removal now would lie (CodeRabbit
            # #466 round 2).
            _schedule_exe_delete(installed)
            failed.append(f"{installed} (scheduled - deletes as this "
                          "process exits; remove by hand if it survives)")
        else:
            try:
                os.remove(installed)
                removed.append(installed)
            except OSError as exc:
                failed.append(f"{installed} ({exc})")

    return removed, failed


def _schedule_exe_delete(installed: str) -> None:
    """Delete the (still-running) installed exe after this process exits.

    Windows refuses to delete a running process's own image, so spawn a
    detached no-window PowerShell that waits, then removes the file.
    """
    import subprocess

    if sys.platform != "win32":
        return
    create_no_window = 0x08000000
    detached = 0x00000008
    esc = installed.replace("'", "''")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Start-Sleep -Seconds 2; Remove-Item -Force "
         "-ErrorAction SilentlyContinue '" + esc + "'"],
        creationflags=create_no_window | detached, close_fds=True)


def _gui_api():
    """The namespace handed to scripts/server_gui.py (which cannot import
    this module back: scripts/ is not a package and the frozen exe runs
    server_entry as __main__ - the namespace keeps the GUI module
    UI-only, every side effect staying in server_entry)."""
    import types

    ns = types.SimpleNamespace(
        InstallError=InstallError,
        _installed_exe=_installed_exe,
        _service_state=_service_state,
        _service_profile_dir=_service_profile_dir,
        _configured_bind=_configured_bind,
        _display_version=_display_version,
        _service_started_at=_service_started_at,
        _open_logs=_open_logs,
        _do_install=_do_install,
        _do_uninstall=_do_uninstall,
    )
    return ns


def _run_gui(which: str, launch_profile: str | None = None) -> int:
    """Import and run one GUI window; report a Tkinter-less build."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("Tkinter is not available in this build - the GUI needs it")
        return 1
    try:
        import server_gui as gui  # frozen build (bundled top-level module)
    except ImportError:
        # Dev tree: load the sibling file (scripts/ is not a package).
        import importlib.util

        gui_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "server_gui.py")
        spec = importlib.util.spec_from_file_location("server_gui", gui_path)
        gui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gui)
    fn = {"setup": gui.run_gui_installer,
          "uninstall": gui.run_gui_uninstaller,
          "status": gui.run_gui_status}[which]
    return fn(_gui_api(), launch_profile)


def _gui_dispatch(args: list[str]) -> int | None:
    """Route the GUI verbs (setup/uninstall/status/logs).

    Console builds keep the old prompts; the windowed build (issue
    #466) needs no console: all four verbs work the same there.
    """
    if not args:
        return None
    verb, rest = args[0], args[1:]
    profile = None
    if rest and not rest[0].startswith("--"):
        profile = rest[0]  # captured launch profile (self-elevation)
    if verb in ("setup", "uninstall") and profile is None:
        # Capture the launching user's home BEFORE the UAC branch: if a
        # standard user elevates with a different admin's credentials,
        # the elevated process's expanduser("~") is the ADMIN's profile,
        # and the passphrase config would land there instead of the
        # user's (the identity anchor the whole wizard is built around,
        # CodeRabbit #466 round 2).
        profile = os.path.expanduser("~")
    if (verb in ("setup", "uninstall") and sys.platform == "win32"
            and not _is_elevated()):
        # Elevate before any UI: the original wizard elevated first, and
        # the Start Menu uninstall shortcut needs it (service removal is
        # an admin operation). The elevated process re-enters here with
        # the pre-elevation profile handed through.
        import ctypes

        arguments = f'{verb} "{profile}"'
        me = os.path.abspath(sys.executable)
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", me, arguments, None, 1)
        if result <= 32:
            print("error: elevation declined - " + (
                "installing the service needs an administrator"
                if verb == "setup" else "removing the service needs an "
                "administrator"))
            return 1
        return 0
    if verb == "setup":
        if _windowed():
            return _run_gui("setup", profile)
        # console build: the old prompts remain available (and tested)
        return _console_wizard(profile)
    if verb == "uninstall":
        return _run_gui("uninstall", profile)
    if verb == "status":
        return _run_gui("status", profile)
    if verb == "logs":
        _open_logs()
        return 0
    return None


def _windowed() -> bool:
    """True in the --windowed build: PyInstaller's bootloader strips
    stdio there (sys.stdout/stderr are None)."""
    return sys.stdout is None or sys.stderr is None


def _console_wizard(launch_profile: str | None = None) -> int:
    """The pre-#466 console prompts, kept for the console build and the
    tests (the GUI is a wrapper, never a replacement: scripted/admin
    installs keep working exactly as before)."""
    import getpass
    import socket

    if sys.platform != "win32":
        print("the setup wizard is Windows-only; on Linux install the .deb")
        return 1
    profile = launch_profile or os.path.expanduser("~")
    print("YAAH headless server setup")
    print("==========================")
    print()
    passphrase = getpass.getpass(
        "passphrase desktop clients must present: ").strip()
    if not passphrase:
        print("error: a passphrase is required (without one the server "
              "refuses all remote access)")
        return 1
    display = input(
        f"display name in the host switcher [{socket.gethostname()}]: ").strip()
    display = display or socket.gethostname()
    print()
    try:
        state = _do_install(passphrase=passphrase, display=display,
                            host="0.0.0.0", port=8765, profile=profile)
    except InstallError as exc:
        print(f"error: {exc}")
        return 1
    if state == "elevated":
        return 0
    print()
    print("done: YAAH Headless Server is installed and running.")
    print("connect from the YAAH desktop app: host switcher (mDNS) or "
          "direct IP:8765 with the passphrase above.")
    installed = _installed_exe()
    print(f"uninstall: `{installed} uninstall` (GUI) or `{installed} remove`")
    return 0

def _service_class():
    """The Windows service wrapper class, or None off Windows / without pywin32."""
    if sys.platform != "win32":
        return None
    try:
        import win32event
        import win32serviceutil
    except ImportError:
        return None

    import threading

    class YaahService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESC

        def __init__(self, args):
            """Initialize the service framework."""
            import servicemanager
            import win32event
            import win32service

            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.log = servicemanager.LogMsg

        def SvcStop(self):
            """Handle the SCM stop signal."""
            # Signal SvcDoRun; without this the SCM stop times out and the
            # service hangs in STOP_PENDING ("Gave up waiting...").
            import win32event
            import win32service

            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.hWaitStop)

        def SvcDoRun(self):
            """Run the service: apply profile dir and start uvicorn."""
            import servicemanager

            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            # Service context has no console: print would raise on a None
            # stdout (uvicorn's loggers write there too). Tee both to the
            # install-dir log file - the "Server Logs" shortcut opens it
            # (resolved from the INSTALLED exe's dir, so LocalSystem and
            # the user land on the same folder; CodeRabbit #466 round 2).
            # Best-effort: an unwritable log must not stop the service.
            try:
                log_dir = _log_dir()
                os.makedirs(log_dir, exist_ok=True)
                log_path = os.path.join(
                    log_dir, time.strftime("yaah-server-%Y%m%d.log"))
                sys.stdout = open(log_path, "a", buffering=1)
                sys.stderr = sys.stdout
            except OSError:
                if sys.stdout is None:
                    sys.stdout = open(os.devnull, "w")
                if sys.stderr is None:
                    sys.stderr = open(os.devnull, "w")
            # uvicorn blocks, so run it aside and wait for the stop event —
            # SvcDoRun must return for the service to stop cleanly. The
            # host is stateless, so an abrupt teardown loses nothing.
            t = threading.Thread(target=self._serve, daemon=True)
            t.start()
            import win32event

            win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
            self.ReportServiceStatus(win32serviceutil.SERVICE_STOPPED)

        def _serve(self):
            """Run uvicorn in a separate thread for clean service shutdown."""
            import servicemanager

            try:
                # Started as LocalSystem: re-point the per-user paths at
                # the profile setup recorded (the passphrase config lives
                # there) before anything resolves Path.home(). mDNS stays
                # on — a service has the same LAN presence as a console
                # run.
                _apply_service_profile_dir()
                host, port = _configured_bind()
                run_server(host, port)
            except Exception as exc:  # noqa: BLE001 - SCM needs a clean exit
                hint = ""
                # Classic headless collision: a YAAH desktop app on the same
                # box holds 8765, uvicorn's bind fails (WinError 10048), the
                # service dies and the SCM restart loop repeats it forever.
                if "10048" in repr(exc) or "bind" in str(exc).lower():
                    hint = (" — port 8765 is already in use: is a YAAH "
                            "desktop app running on this box? stop it, or "
                            "serve on another port")
                servicemanager.LogErrorMsg(
                    f"{SERVICE_NAME} crashed: {exc!r}{hint}")
                self.ReportServiceStatus(win32serviceutil.SERVICE_STOPPED)

    return YaahService


def _service_profile_dir() -> str:
    """The installing user's profile recorded in the service's
    Parameters key, or "" when absent/unreadable/deleted.

    The GUI windows use this to read and remove the RIGHT user's
    config: the elevated process's expanduser("~") is the admin's home
    whenever a standard user installed with different elevation
    credentials (CodeRabbit #466 round 2). Read-only - unlike
    _apply_service_profile_dir it touches no env vars.
    """
    try:
        import winreg

        with winreg.OpenKeyEx(
            winreg.HKEY_LOCAL_MACHINE, PARAM_SUBKEY + r"\Parameters", 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            profile, _vtype = winreg.QueryValueEx(key, PROFILE_DIR_VALUE)
    except (ImportError, OSError):
        return ""
    profile = str(profile).strip()
    return profile if profile and os.path.isdir(profile) else ""


def _apply_service_profile_dir() -> bool:
    """Point per-user path resolution at the installing user's profile.

    The wizard stored the profile directory in the service's Parameters
    key (PARAM_SUBKEY + Parameters, value ProfileDir, REG_SZ) at install
    time. Reading it here - before any backend import, since
    backend.agent.config resolves its config path via Path.home() at
    module import - makes USERPROFILE, HOMEDRIVE/HOMEPATH and HOME agree
    on that directory: the passphrase config AND the advertised
    workspace_root become the installing user's real home instead of
    SYSTEM's systemprofile.

    Best-effort by design: a missing or unreadable value leaves the
    SYSTEM defaults in place (the server then runs with no passphrase
    and refuses every remote request - the documented config-less
    state).
    """
    profile = _service_profile_dir()
    if not profile:
        return False
    norm = os.path.normpath(profile)
    drive, rest = os.path.splitdrive(norm)
    os.environ["USERPROFILE"] = norm
    os.environ["HOMEDRIVE"] = drive or norm[:2]
    os.environ["HOMEPATH"] = rest or "\\"
    os.environ["HOME"] = norm
    return True


def _handle_service_command(argv: list[str]) -> int | None:
    """Route service verbs (install/remove/start/stop/restart)."""
    """Dispatch `install|remove|start|stop|restart` (and bare SCM start).

    Returns the exit code, or None if argv is not a service invocation.
    """
    cls = _service_class()
    if cls is None:
        if argv and argv[0] in ("install", "remove", "start", "stop", "restart"):
            print("service commands require Windows with pywin32 installed")
            return 1
        return None
    import win32serviceutil

    verbs = {"install", "remove", "start", "stop", "restart"}
    if not argv:
        # Started by the Windows SCM: host the service control dispatcher.
        # If we were NOT started by the SCM (double-click), fall through to
        # the setup wizard — friendlier than a cryptic error.
        import servicemanager
        import pywintypes

        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(cls)
        try:
            servicemanager.StartServiceCtrlDispatcher()
            return 0
        except pywintypes.error as exc:
            if exc.winerror == 1063:  # ERROR_FAILED_SERVICE_CONTROLLER_CONNECT
                # Double-click: GUI wizard in the windowed build, the old
                # console prompts in the console build (_gui_dispatch).
                return _gui_dispatch(["setup"])
            raise
    # HandleCommandLine's own convention puts flags BEFORE the verb
    # (`exe --startup auto install --username …`), so look for the verb
    # anywhere in argv, not just at the front.
    # Clean firewall rules when removing (issue #465).
    if "remove" in argv:
        win32serviceutil.HandleCommandLine(cls, argv=[""] + argv)
        _configure_firewall(_installed_exe(), remove=True)
        # The ARP entry and Start Menu folder describe the install; the
        # GUI uninstaller removes them through _do_uninstall, so a bare
        # `remove` must not leave either behind (issue #466).
        _delete_arp()
        _delete_start_menu()
        return 0
    if any(a in verbs for a in argv):
        win32serviceutil.HandleCommandLine(cls, argv=[""] + argv)
        return 0
    return None


def _persist_remote(updates: dict, profile_dir: str | None = None) -> None:
    """Merge updates into the remote-access config file.

    If profile_dir is provided, write directly to that user's config
    (needed when the wizard elevates as a different admin). Otherwise
    use the current process's resolved config path.
    """
    import json
    import os

    if profile_dir is not None:
        cfg_path = os.path.join(os.path.normpath(profile_dir), '.yaah', 'config.json')
    else:
        from backend.agent.config import CONFIG_PATH
        cfg_path = CONFIG_PATH

    cfg = {}
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    remote = cfg.get('remote') or {}
    remote.update(updates)
    cfg['remote'] = remote

    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)


def _is_elevated() -> bool:
    """Check if this process has Administrator (elevated) privileges."""
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _installed_exe() -> "os.PathLike | str":
    r"""Path to the installed server executable (%LOCALAPPDATA%\YAAH)."""
    """Where this exe lives when installed (%LOCALAPPDATA%\\YAAH)."""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, WIN_INSTALL_DIR, "yaah-server-setup.exe")


def _netsh_firewall(*args):
    """Run a netsh advfirewall firewall command.

    Returns (ok, stdout): ok is True when netsh exited 0, stdout is
    the command's output.
    """
    import subprocess

    cmd = ["netsh", "advfirewall", "firewall"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False,
                            creationflags=_no_window())
    return result.returncode == 0, result.stdout


# (rule name, protocol, port) for the two inbound rules YAAH needs.
FIREWALL_RULES = [
    ("YAAH Server", "TCP", "8765"),
    ("YAAH mDNS", "UDP", "5353"),
]


def _configure_firewall(installed: str, remove: bool = False,
                        port: int = 8765, emit=None) -> None:
    """Add or remove Windows Firewall inbound rules for the YAAH server.

    Two rules are managed (FIREWALL_RULES):
      - "YAAH Server" - TCP (remote-exec API; the INSTALL's port, not a
        hardcoded 8765 - a custom port with an 8765 hole invites
        debugging pain, CodeRabbit #466 round 2)
      - "YAAH mDNS"   - UDP 5353 (mDNS advertising, in-process beacon)

    Rules are scoped to the installed exe's path so only that process
    can bind these ports. Both Public and Private profiles are enabled.

    On setup each rule is deleted before it is added: delete is a
    harmless no-op when the rule does not exist, and it guarantees a
    stale same-named rule (e.g. one pinned to a Downloads copy of the
    exe left by the manual workaround) can never shadow the clean
    rule. Locale-proof: no output parsing, exit codes only.
    """
    said = emit or print
    for name, proto, rule_port in FIREWALL_RULES:
        ok, _out = _netsh_firewall("delete", "rule",
                                   "name={}".format(name))
        if remove:
            if ok:
                said("removed firewall rule: {}".format(name))
            else:
                said("warning: no rule removed for {} - if rules "
                     "exist, re-run remove from an elevated "
                     "prompt".format(name))
            continue
        effective = str(port) if name.startswith("YAAH Server") else rule_port
        ok, _out = _netsh_firewall("add", "rule",
                                   "name={}".format(name),
                                   "dir=in", "action=allow",
                                   "protocol={}".format(proto),
                                   "localport={}".format(effective),
                                   "program={}".format(installed),
                                   "profile=public,private", "enable=yes")
        if ok:
            said("firewall rule in place: {} (port {})".format(
                name, effective))
        else:
            said("warning: could not create firewall rule: {}".format(
                name))
            said("manual fix: run setup again from an elevated prompt")


def _service_state() -> str:
    """The installed service's current state ("" when not installed)."""
    import subprocess

    out = subprocess.run(
        ["sc", "query", SERVICE_NAME], capture_output=True, text=True,
        check=False, creationflags=_no_window(),
    ).stdout
    for line in out.splitlines():
        if "STATE" in line:
            return line.split(None, 3)[-1].strip()  # e.g. "4  RUNNING"
    return ""


def _start_and_await(installed: str, timeout: float = 10.0) -> str:
    """Start the service and poll the SCM until it reaches a terminal state."""
    """`start` the service, then poll the SCM until it settles.

    Returns the final state string ("4  RUNNING" on success). pywin32's
    HandleCommandLine always exits 0 — its return code says nothing about
    whether the service actually came up, so the SCM is the only truth.
    """
    import subprocess
    import time

    subprocess.run([installed, "start"], check=False,
                   creationflags=_no_window())
    deadline = time.time() + timeout
    while True:
        state = _service_state()
        if state.endswith("RUNNING") or (state and "PENDING" not in state):
            return state
        if time.time() >= deadline:
            return state
        time.sleep(0.5)


def _run_setup_wizard(launch_profile: str | None = None) -> int:
    """Console install flow (kept for the console build and scripts; the
    GUI wizard wraps the same _do_install sequence - issue #466).

    If launch_profile is provided, it is the launching user's profile
    (captured before UAC elevation); otherwise the wizard captures it
    now and, if not elevated, self-elevates and re-invokes with the
    profile passed through.
    """
    return _console_wizard(launch_profile)


def main(argv: list[str] | None = None) -> int:
    """Foreground serve entry: parse flags, persist, run (the
    systemd/`serve` path; see the module docstring for the verbs)."""
    parser = argparse.ArgumentParser(
        description="YAAH host: serves the remote-exec API that a YAAH "
        "desktop app connects to over the LAN.",
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address (default: 0.0.0.0, LAN hosting)")
    parser.add_argument("--port", type=int, default=8765,
                        help="listen port (default: 8765) — the desktop app "
                        "assumes 8765 for direct-IP entries")
    parser.add_argument("--passphrase", default=None,
                        help="passphrase remote clients must present; saved "
                        "to config.json (same store the desktop app uses)")
    parser.add_argument("--display-name", default=None,
                        help="name shown in the client's host switcher; "
                        "saved to config.json")
    parser.add_argument("--no-hosting", action="store_true",
                        help="skip mDNS advertising; reachable by direct IP only")
    args = parser.parse_args(argv)

    if args.passphrase is not None:
        _persist_remote({"passphrase": args.passphrase})
        print("passphrase saved to config; remote exec enabled")
    if args.display_name is not None:
        _persist_remote({"display_name": args.display_name})
        print(f"display name saved: {args.display_name}")

    # Per-run markers the backend lifespan reads (backend.main):
    #   YAAH_HEADLESS    skip the Windows-only computer-use hooks — there is
    #                    no interactive session to listen on a headless box.
    #   YAAH_NO_HOSTING  skip the mDNS beacon (--no-hosting); direct IP only.
    #   YAAH_API_PORT    the port this process binds (the beacon advertises it).
    if args.no_hosting:
        os.environ["YAAH_NO_HOSTING"] = "1"

    run_server(args.host, args.port)
    return 0


def entry(argv: list[str] | None = None) -> int:
    """Console entry point: GUI verbs, service commands, then CLI."""
    args = sys.argv[1:] if argv is None else argv

    if args and args[0] in ("setup", "uninstall", "status", "logs"):
        # args[1] may be the captured launch profile (self-elevation).
        return _gui_dispatch(args)
    if args and args[0] == "serve":  # explicit foreground run
        serve_args = list(args[1:])
        if "--port" not in serve_args and sys.platform == "win32":
            # Match the service: the recorded (or default) port, not a
            # hardcoded 8765. run_server sets YAAH_API_PORT either way.
            serve_args = ["--port", str(_configured_port())] + serve_args
        return main(serve_args)
    if sys.platform == "win32" and not args:
        # Bare start on Windows: SCM service entry, or wizard on
        # double-click (fallback inside _handle_service_command). Without
        # pywin32 in the build, serve in the foreground.
        service_rc = _handle_service_command(args)
        return service_rc if service_rc is not None else main(args)
    service_rc = _handle_service_command(args)
    if service_rc is not None:
        return service_rc
    return main(args)


if __name__ == "__main__":
    sys.exit(entry())
