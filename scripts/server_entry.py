"""Headless YAAH server entry point (``yaah-server``).

Serves the same FastAPI app the desktop sidecar runs (backend.main:app),
but as a plain console executable: no Tauri, no webview, no GUI. A YAAH
desktop instance connects to it over the LAN exactly as it would connect
to another desktop app — the host role (handshake, workspace-tool exec,
passphrase gate, mDNS beacon) is already implemented in backend.main.

The host is stateless by design: conversations, provider keys and the
agent loop stay on the client. The server only executes workspace tools
(shell, files, git) in its own workspace (its home directory), so this
binary needs none of the GUI-only weight (voice, Windows-only computer
use) the desktop sidecar bundles.

CLI:
    yaah-server [--host H] [--port P] [--passphrase SECRET]
                [--display-name NAME] [--no-hosting]

    yaah-server install | remove | start | stop | restart
        Windows service management (Windows builds only; needs an elevated
        prompt). The service is registered with automatic startup and runs
        as the account given via --username/--password (the setup wizard
        passes these for you): the workspace is that user's real home.

    yaah-server-setup
        Interactive wizard (separate exe, Windows builds): prompts for a
        passphrase + display name, writes ~/.yaah/config.json, then
        installs/starts the service as the current user.

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
        import servicemanager
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
                # Passphrase comes from ~/.yaah/config.json (the setup wizard
                # writes it as the user); mDNS stays on — a service has the
                # same LAN presence as a console run.
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
        import servicemanager

        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(cls)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    if argv[0] in verbs:
        win32serviceutil.HandleCommandLine(cls, argv=[""] + argv)
        return 0
    return None


def _persist_remote(updates: dict) -> None:
    """Merge updates into the ``remote`` section of the shared config."""
    from backend.agent.config import load_config, save_config

    cfg = load_config().get("remote") or {}
    save_config({"remote": {**cfg, **updates}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yaah-server",
        description="Headless YAAH host: serves the remote-exec API that a "
        "YAAH desktop app connects to over the LAN.",
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
    """Console entry point: service commands first, then CLI parsing."""
    args = sys.argv[1:] if argv is None else argv
    service_rc = _handle_service_command(args)
    if service_rc is not None:
        return service_rc
    return main(args)


if __name__ == "__main__":
    sys.exit(entry())
