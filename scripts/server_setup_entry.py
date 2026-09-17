"""Interactive Windows installer for the YAAH headless server (``yaah-server-setup``).

Walks an admin through configuring and installing the Windows service:

  1. prompts for the passphrase clients must present (and a display name),
     saved to ~/.yaah/config.json of the *current* user;
  2. registers the ``YaahServer`` service pointing at the ``yaah-server.exe``
     sitting next to this wizard, with automatic startup, running as the
     current user (so the daemon's workspace is the real home directory);
  3. sets failure recovery (auto-restart) and starts the service.

Run from an elevated prompt (the service install requires it); the wizard
checks and says so otherwise. There is no Python on the target machine —
this ships as a PyInstaller exe built by scripts/build_server.sh.
"""

import ctypes
import getpass
import socket
import subprocess
import sys
from pathlib import Path


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _server_exe() -> Path:
    """yaah-server.exe is staged next to the setup wizard in the zip."""
    exe = Path(sys.executable).resolve().parent / "yaah-server.exe"
    if not exe.exists():
        print(f"error: {exe} not found — keep yaah-server-setup.exe next to yaah-server.exe")
        sys.exit(1)
    return exe


def main() -> int:
    if sys.platform != "win32":
        print("the setup wizard is Windows-only; on Linux install the .deb")
        return 1
    if not _is_elevated():
        print("error: run from an elevated (Administrator) prompt — "
              "installing a Windows service requires it")
        return 1

    server = _server_exe()
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

    from server_entry import _persist_remote

    _persist_remote({"passphrase": passphrase, "display_name": display})
    print(f"saved passphrase + display name ({display!r}) to "
          f"%USERPROFILE%\\.yaah\\config.json")

    # Register + start the service as the current user. HandleCommandLine
    # (inside yaah-server.exe) owns the exact install semantics; --startup
    # auto = automatic start at boot.
    print(f"installing service as {user} (automatic startup)...")
    rc = subprocess.run(
        [str(server), "--startup", "auto", "install",
         "--username", f".\\{user}", "--password", getpass.getpass(
             f"Windows password for {user}: ")],
    ).returncode
    if rc != 0:
        print(f"error: service install failed (exit {rc})")
        return rc

    # Auto-restart on crash (first two failures after 5s, then every 5s
    # within a day). Best-effort: older `sc` builds differ in arg spacing.
    subprocess.run(
        ["sc", "failure", "YaahServer", "reset=", "86400",
         "actions=", "restart/5000/restart/5000/restart/5000"],
        capture_output=True,
    )

    rc = subprocess.run([str(server), "start"]).returncode
    if rc != 0:
        print("error: service installed but failed to start — check "
              "Services.msc → YAAH Headless Server")
        return rc

    print()
    print("done: YAAH Headless Server is installed and running.")
    print("connect from the YAAH desktop app: host switcher (mDNS) or "
          "direct IP:8765 with the passphrase above.")
    print("uninstall: `yaah-server remove` from an elevated prompt "
          "(stops + deregisters; the passphrase stays in config.json).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
