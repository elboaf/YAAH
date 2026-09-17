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

--passphrase / --display-name persist to the same ~/.yaah/config.json the
desktop app's Settings writes, so they survive restarts and the desktop
app sees the same values. Without a passphrase the server still starts
(and is discoverable), but every remote exec request is refused with 401
— the same rule the desktop app applies.
"""

import argparse
import os
import sys


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
    os.environ["YAAH_HEADLESS"] = "1"
    if args.no_hosting:
        os.environ["YAAH_NO_HOSTING"] = "1"

    import uvicorn

    from backend.agent.remote import PROTOCOL_VERSION
    from backend.main import app

    print(f"yaah-server listening on {args.host}:{args.port} (protocol {PROTOCOL_VERSION})")
    print("connect from YAAH desktop: host switcher (mDNS) or direct IP:port")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
