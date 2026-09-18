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
management, and the interactive setup wizard):

    yaah-server-setup setup
        Interactive installer: prompts for the passphrase + display name,
        copies itself to %LOCALAPPDATA%\\YAAH\\, registers the Windows
        service (automatic startup, running as your user so the workspace
        is your real home), and starts it. Needs an elevated prompt.
        This is also what happens when you double-click the exe.
    yaah-server-setup install|remove|start|stop|restart
        Service management (elevated prompt).
    yaah-server-setup serve [--host H] [--port P] [--passphrase SECRET]
                [--display-name NAME] [--no-hosting]
        Foreground console run.
    (bare ``yaah-server-setup`` with no arguments is the SCM service
    entry point on Windows; on Linux it serves in the foreground — that
    is what the systemd unit runs.)

On Linux the binary is ``/usr/bin/yaah-server`` inside the .deb (unit
``yaah-server@<user>``, passphrase via /etc/yaah/yaah.conf).

--passphrase / --display-name persist to the same ~/.yaah/config.json the
desktop app's Settings writes, so they survive restarts and the desktop
app sees the same values. Without a passphrase the server still starts
(and is discoverable), but every remote exec request is refused with 401
— the same rule the desktop app applies.
"""

import argparse
import os
import sys


SERVICE_NAME = "YaahServer"
SERVICE_DISPLAY = "YAAH Headless Server"
SERVICE_DESC = "YAAH remote-exec host: serves workspace tools to YAAH desktop clients on the LAN."
WIN_INSTALL_DIR = "YAAH"


def run_server(host: str, port: int) -> None:
    """Run the API server in the foreground (console or service context)."""
    os.environ.setdefault("YAAH_HEADLESS", "1")

    import uvicorn

    from backend.agent.remote import PROTOCOL_VERSION
    from backend.main import app

    print(f"yaah-server listening on {host}:{port} (protocol {PROTOCOL_VERSION})")
    print("connect from YAAH desktop: host switcher (mDNS) or direct IP:port")
    uvicorn.run(app, host=host, port=port, log_level="info")


def _service_class():
    """The Windows service wrapper class, or None off Windows / without pywin32."""
    if sys.platform != "win32":
        return None
    try:
        import win32serviceutil
    except ImportError:
        return None

    class YaahService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESC

        def SvcStop(self):
            self.ReportServiceStatus(win32serviceutil.SERVICE_STOP_PENDING)
            win32serviceutil.ServiceFramework.SvcStop(self)

        def SvcDoRun(self):
            import servicemanager

            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            try:
                # Passphrase comes from ~/.yaah/config.json (setup wrote it
                # as the user); mDNS stays on — a service has the same LAN
                # presence as a console run.
                run_server("0.0.0.0", 8765)
            except Exception as exc:  # noqa: BLE001 - SCM needs a clean exit
                servicemanager.LogErrorMsg(f"{SERVICE_NAME} crashed: {exc!r}")
                self.ReportServiceStatus(win32serviceutil.SERVICE_STOPPED)

    return YaahService


def _handle_service_command(argv: list[str]) -> int | None:
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
                return _run_setup_wizard()
            raise
    # HandleCommandLine's own convention puts flags BEFORE the verb
    # (`exe --startup auto install --username …`), so look for the verb
    # anywhere in argv, not just at the front.
    if any(a in verbs for a in argv):
        win32serviceutil.HandleCommandLine(cls, argv=[""] + argv)
        return 0
    return None


def _persist_remote(updates: dict) -> None:
    """Merge updates into the ``remote`` section of the shared config."""
    from backend.agent.config import load_config, save_config

    cfg = load_config().get("remote") or {}
    save_config({"remote": {**cfg, **updates}})


def _is_elevated() -> bool:
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _installed_exe() -> "os.PathLike | str":
    """Where this exe lives when installed (%LOCALAPPDATA%\\YAAH)."""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, WIN_INSTALL_DIR, "yaah-server-setup.exe")


def _run_setup_wizard() -> int:
    """Interactive Windows install: prompts, self-copy, service, start.

    Runs in-process in the same single exe that hosts the service; there
    is no companion installer binary.
    """
    import getpass
    import shutil
    import socket
    import subprocess

    if sys.platform != "win32":
        print("the setup wizard is Windows-only; on Linux install the .deb")
        return 1
    if not _is_elevated():
        print("error: run from an elevated (Administrator) prompt — "
              "installing a Windows service requires it")
        return 1

    me = os.path.abspath(sys.executable)
    installed = _installed_exe()
    # Self-copy: the service must not depend on this exe staying wherever
    # the user happened to run it from (e.g. Downloads).
    if os.path.normcase(me) != os.path.normcase(installed):
        os.makedirs(os.path.dirname(installed), exist_ok=True)
        shutil.copy2(me, installed)
        print(f"installed server to {installed}")

    user = getpass.getuser()

    print("YAAH headless server setup")
    print("==========================")
    print()
    passphrase = getpass.getpass("passphrase desktop clients must present: ").strip()
    if not passphrase:
        print("error: a passphrase is required (without one the server refuses "
              "all remote access)")
        return 1
    display = input(f"display name in the host switcher [{socket.gethostname()}]: ").strip()
    display = display or socket.gethostname()
    print()

    _persist_remote({"passphrase": passphrase, "display_name": display})
    print(f"saved passphrase + display name ({display!r}) to "
          f"%USERPROFILE%\\.yaah\\config.json")

    # Register + start the service as the current user. HandleCommandLine
    # owns the exact install semantics; --startup auto = start at boot.
    print(f"installing service as {user} (automatic startup)...")
    rc = subprocess.run(
        [installed, "--startup", "auto", "install",
         "--username", f".\\{user}", "--password", getpass.getpass(
             f"Windows password for {user}: ")],
    ).returncode
    if rc != 0:
        print(f"error: service install failed (exit {rc})")
        return rc

    # Auto-restart on crash (every failure, 5s apart, counters reset daily).
    # Best-effort: arg spacing varies across sc builds.
    subprocess.run(
        ["sc", "failure", SERVICE_NAME, "reset=", "86400",
         "actions=", "restart/5000/restart/5000/restart/5000"],
        capture_output=True,
    )

    rc = subprocess.run([installed, "start"]).returncode
    if rc != 0:
        print("error: service installed but failed to start — check "
              "Services.msc → " + SERVICE_DISPLAY)
        return rc

    print()
    print("done: YAAH Headless Server is installed and running.")
    print("connect from the YAAH desktop app: host switcher (mDNS) or "
          "direct IP:8765 with the passphrase above.")
    print(f"uninstall: `{installed} remove` from an elevated prompt "
          "(stops + deregisters; delete the config to wipe the passphrase).")
    return 0


def main(argv: list[str] | None = None) -> int:
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
    if args.no_hosting:
        os.environ["YAAH_NO_HOSTING"] = "1"

    run_server(args.host, args.port)
    return 0


def entry(argv: list[str] | None = None) -> int:
    """Console entry point: setup / service commands first, then CLI."""
    args = sys.argv[1:] if argv is None else argv

    if args and args[0] == "setup":
        return _run_setup_wizard()
    if args and args[0] == "serve":  # explicit foreground run
        return main(args[1:])
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
