# YAAH headless server — installation guide

The `yaah-server-setup` artifact turns any machine into a **host**: it runs
the remote-exec API (shell, files, git) as a background daemon that boots
with the machine — no command prompt, no YAAH desktop app, no Python.
Conversations and provider keys always stay on the client; the host is a
stateless executor working in **the user's home directory**.

Downloads are on the [Releases](../../releases) page — exactly four
artifacts:

| | Windows | Linux (deb) |
|---|---|---|
| Desktop app | `yaah-desktop-setup.exe` | `yaah-desktop-setup.deb` |
| Headless server | `yaah-server-setup.exe` | `yaah-server-setup.deb` |

Trust model: the daemon executes shell commands on the host as the account
it runs as — your user on Linux, LocalSystem on Windows — gated by a
passphrase only. Install it on machines you control.

## Windows

1. Download `yaah-server-setup.exe` and double-click it (or run it from
   a prompt — same thing). A UAC prompt appears (admin account: yes/no;
   standard account: enter an administrator's password), then the
   **setup window** opens. Installing a service needs that elevation.
2. Fill the form and click **Install**:
   - the **passphrase** desktop clients must present,
   - a **display name** (what the client's host switcher shows; defaults
     to the hostname),
   - the **port** (default 8765 — what direct-IP entries assume) and
     **host bind** (default `0.0.0.0` = LAN hosting).
   An existing install is detected and pre-filled (upgrade, not reinstall).
3. The installer copies itself to `%LOCALAPPDATA%\YAAH\`, registers the
   `YaahServer` service (automatic startup, running as **LocalSystem** —
   no account password involved), records the installing user's profile
   in the service's `Parameters` registry key, adds the Windows Firewall
   rules, and starts the service. A confirmation dialog closes it out.
4. The server now appears in **Programs & Features** ("YAAH Headless
   Server", right-click — Uninstall) and the Start Menu gains a
   **YAAH Server** folder: *Uninstall*, *Open Server Status* (running
   state, port, version, last start) and *Server Logs*.

Run setup from the account whose workspace you want the server to serve:
the passphrase is stored in that account's `~/.yaah/config.json` and the
service reads it from there. (If the elevation popup was answered with a
*different* admin's credentials, the recorded profile is the account the
elevated process runs under — the one you typed into the popup, not the
one that launched the exe.)

Because the service runs as LocalSystem there is no stored account
password to go stale: changing your Windows password, or using a
passwordless account, cannot break the service — that is the whole point
of the design. The tradeoff is deliberate: the passphrase now gates full
command of the machine (as SYSTEM), regardless of who installed it.

First-run firewall note: if you ever ran the exe manually from Downloads,
Windows' allow rule is pinned to that path. The service runs from
`%LOCALAPPDATA%\YAAH\` and never gets an interactive prompt, so allow it
explicitly (elevated):

    netsh advfirewall firewall add rule name="YAAH Server" dir=in action=allow program="C:\Users\<you>\AppData\Local\YAAH\yaah-server-setup.exe" enable=yes

Manage the server:

    yaah-server-setup status      # status window (or: Start Menu > Open Server Status)
    yaah-server-setup logs        # open the logs folder in Explorer
    yaah-server-setup uninstall   # GUI uninstaller (same as Programs & Features)

Manage the service from a script (elevated prompt; the verbs the GUI wraps):

    yaah-server-setup start | stop | restart
    yaah-server-setup remove      # stop, deregister, drop ARP + Start Menu
    yaah-server-setup serve       # foreground console run (debugging)

Verify: `curl http://127.0.0.1:8765/api/remote/info` — `workspace_root`
must be the installing user's home. If it says
`...system32\config\systemprofile`, the service could not read its
`Parameters\ProfileDir` value (setup prints a warning when that write
fails) — re-run setup.

Two events in the System log look scary but aren't failures:

- **7039** ("a service process other than the one launched by the Service
  Control Manager connected") — the PyInstaller bootloader parent spawns
  the real process, which then connects to the SCM. Windows logs the
  mismatch and proceeds; the service runs.
- **One start of the service and one of the desktop app can't share port
  8765.** If a YAAH desktop app is running on the box, the service crashes
  at bind ("service-specific error 1" + a `YaahServer crashed` line in the
  Application log naming the port). Stop the desktop app (or don't install
  the service on a desktop-use box); the SCM restart policy retries every
  5 s, so the service picks the port up once it frees.

## Uninstalling (Windows)

Programs & Features — right-click "YAAH Headless Server" — Uninstall (or
Start Menu — YAAH Server — Uninstall YAAH Server). The uninstaller asks,
one by one, whether to remove the service (+ firewall rules + Start Menu
folder), the YAAH data folder (`~/.yaah` — shared with the desktop app on
this box, so it is opt-in and stays by default), and the program file;
it shows the exact summary before anything is deleted, then reports what
was removed. Unattended equivalent: `yaah-server-setup remove` from an
elevated prompt — in the windowed build the service host has no console,
so this prints nothing and `HandleCommandLine` may return before the
 SCM finishes the stop; wait for the SCM yourself (poll `sc query
YaahServer` until it leaves RUNNING) before reusing the port or
reinstalling (CodeRabbit #466 round 2).

## Linux (deb)

    curl -LO https://github.com/elboaf/YAAH/releases/download/v<TAG>/yaah-server-setup.deb
    sudo apt install ./yaah-server-setup.deb

The package installs `/usr/bin/yaah-server`, a systemd template unit
(`yaah-server@.service`), and `/etc/yaah/yaah.conf.example`, then enables
and starts the daemon for the first real login user it finds. To use a
different account: `sudo systemctl enable --now yaah-server@<user>`.

Configure the passphrase (the daemon runs without one but refuses every
remote request — discoverable, harmless, useless):

    sudo cp /etc/yaah/yaah.conf.example /etc/yaah/yaah.conf
    sudo nano /etc/yaah/yaah.conf        # set: YAAH_PASSPHRASE=your-secret
    sudo chmod 640 /etc/yaah/yaah.conf   # root + the daemon's user only
    sudo systemctl restart yaah-server@<youruser>

That conf file is the only daemon-specific setting; everything else lives
in the daemon user's `~/.yaah/config.json` as usual.

Verify:

    systemctl status yaah-server@<youruser>
    curl http://127.0.0.1:8765/api/remote/info   # app_version + your home as workspace_root

Upgrades: install the new deb over the old one — the unit and
`/etc/yaah/yaah.conf` are untouched. Removal: `sudo apt remove yaah-server`
(stops and disables all instances; your conf file stays).

## Connecting from the desktop app

Open the **host switcher** — the host appears automatically via mDNS — or
enter `ip:8765` directly, and give the passphrase you configured. If the
host is listed but the password "doesn't work", check the verify output on
the host itself (the curl above): "no passphrase set" means the service
could not resolve its recorded profile dir (re-run setup); "wrong
passphrase" means a plain mismatch.

## Building the artifacts

`scripts/build_server.sh` builds the PyInstaller exe(s); on Windows it
produces the single self-contained `yaah-server-setup.exe` (service host +
GUI installer/uninstaller + status window + management verbs + `serve`,
built `--windowed` so no console flashes), on Linux the plain
`yaah-server` binary that `scripts/build_server_deb.sh` wraps into the
deb. The release workflow builds and attaches all four artifacts on tag
push.
