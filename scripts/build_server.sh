#!/usr/bin/env bash
# Build the headless YAAH server (yaah-server) into a standalone PyInstaller
# executable: dist/yaah-server[.exe], plus a short README next to it.
#
# Unlike build_sidecar.sh (the desktop sidecar), this build carries no voice
# (sherpa_onnx/numpy), no computer use (pynput/mss/uiautomation/comtypes/PIL)
# and no curl_cffi — a remote client only forwards REMOTE_TOOLS (shell, files,
# git) to its host, so a headless box needs none of that. Web tools stay
# client-local and degrade gracefully without curl_cffi (documented soft dep).
#
# No whisper stage: the server build is a single PyInstaller pass.
set -euo pipefail
cd "$(dirname "$0")/.."

pip install -r requirements.txt -r requirements-build.txt

EXE=""
[ "$(uname -s)" = "MINGW" -o "$(uname -s)" = "Windows_NT" ] && EXE=".exe" || true

# Windows service support (YaahService in server_entry.py + the setup
# wizard exe): pywin32 only exists on Windows builds.
PYWIN32_ARGS=()
if [ -n "$EXE" ]; then
  pip install pywin32
  PYWIN32_ARGS=(--hidden-import win32timezone --hidden-import win32serviceutil
    --hidden-import win32service --hidden-import win32event
    --hidden-import servicemanager)
fi

pyinstaller --noconfirm --clean --onefile --console \
  --name yaah-server \
  --paths . --paths scripts \
  --collect-all uvicorn --collect-all fastapi \
  --collect-all pydantic --collect-all pydantic_core \
  --collect-all anyio --collect-all aiosqlite \
  --collect-all httpx --collect-all httpcore \
  --collect-all zeroconf \
  --hidden-import mcp --hidden-import mcp.client.stdio \
  --hidden-import mcp.client.session --hidden-import mcp.types \
  --hidden-import yaml \
  --exclude-module sherpa_onnx --exclude-module numpy \
  --exclude-module curl_cffi --exclude-module pynput \
  --exclude-module mss --exclude-module uiautomation \
  --exclude-module comtypes --exclude-module PIL \
  "${PYWIN32_ARGS[@]}" \
  scripts/server_entry.py

# Windows-only companion: the interactive service installer wizard.
if [ -n "$EXE" ]; then
  pyinstaller --noconfirm --clean --onefile --console \
    --name yaah-server-setup \
    --paths . --paths scripts \
    --hidden-import server_entry \
    "${PYWIN32_ARGS[@]}" \
    scripts/server_setup_entry.py
  echo "setup wizard: dist/yaah-server-setup.exe"
fi

echo "server: dist/yaah-server$EXE"

# ---- README staged next to the exe (also attached in release.yml) ----
# Repo root, NOT dist/: dist/ is vite's outDir and `tauri build` wipes it.
cat > README-server.txt <<'EOF'
YAAH headless server
====================

Run on the target device (no GUI, no Python needed):

    yaah-server --passphrase <secret>

Then connect from the YAAH desktop app: open the host switcher (the
server appears automatically on the LAN) or enter the device's IP
directly (default port 8765), using the same passphrase.

Options:
  --host H            bind address (default 0.0.0.0)
  --port P            listen port (default 8765)
  --passphrase SECRET save the passphrase clients must present
  --display-name NAME name shown in the desktop app's host switcher
  --no-hosting        skip LAN discovery; connect by direct IP only

The passphrase and display name are saved to ~/.yaah/config.json — the
same store the desktop app uses — so they survive restarts. The server
executes workspace tools (shell, files, git) in its own home directory;
conversations and provider keys stay on the desktop app.

Install as a background service
-------------------------------
Windows: run yaah-server-setup.exe (from this zip) in an elevated prompt.
It prompts for a passphrase, registers the "YAAH Headless Server" service
(automatic startup, runs as your user so the workspace is your home),
and starts it. Manage with: yaah-server start|stop|remove.

Linux: install the yaah-server .deb. It drops /usr/bin/yaah-server, a
systemd template unit (enabled + started for your login user), and
/etc/yaah/yaah.conf.example. Configure:

    sudo sh -c 'echo "YAAH_PASSPHRASE=your-secret" > /etc/yaah/yaah.conf'
    sudo chmod 640 /etc/yaah/yaah.conf
    sudo systemctl restart yaah-server@<youruser>

With no /etc/yaah/yaah.conf the daemon still runs and is discoverable,
but refuses every remote request (no passphrase = no access).
EOF
echo "readme: README-server.txt"
