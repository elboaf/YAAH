#!/usr/bin/env bash
# Build the headless YAAH server (scripts/server_entry.py) into standalone
# PyInstaller executables.
#
# One self-contained artifact per platform — no companion files:
#   Windows: dist/yaah-server-setup.exe  (service host + `setup` wizard +
#            install/remove/start/stop/restart + foreground `serve`)
#   Linux:   dist/yaah-server            (binary packaged into the .deb by
#            scripts/build_server_deb.sh)
#
# Unlike build_sidecar.sh (the desktop sidecar), this build carries no voice
# (sherpa_onnx/numpy), no computer use (pynput/mss/uiautomation/comtypes/PIL)
# and no curl_cffi — a remote client only forwards REMOTE_TOOLS (shell, files,
# git) to its host, so a headless box needs none of that. Web tools stay
# client-local and degrade gracefully without curl_cffi (documented soft dep).
set -euo pipefail
cd "$(dirname "$0")/.."

pip install -r requirements.txt -r requirements-build.txt

EXE=""
case "$(uname -s)" in
  MINGW*|MSYS*|Windows_NT*|CYGWIN*) EXE=".exe" ;;
esac

if [ -n "$EXE" ]; then
  # Windows: the single exe hosts the service (pywin32) AND the setup
  # wizard (`yaah-server-setup setup` / double-click).
  pip install pywin32
  pyinstaller --noconfirm --clean --onefile --console \
    --name yaah-server-setup \
    --paths . --paths scripts \
    --collect-all uvicorn --collect-all fastapi \
    --collect-all pydantic --collect-all pydantic_core \
    --collect-all anyio --collect-all aiosqlite \
    --collect-all httpx --collect-all httpcore \
    --collect-all zeroconf \
    --hidden-import mcp --hidden-import mcp.client.stdio \
    --hidden-import mcp.client.session --hidden-import mcp.types \
    --hidden-import yaml \
    --hidden-import win32timezone --hidden-import win32serviceutil \
    --hidden-import win32service --hidden-import win32event \
    --hidden-import servicemanager \
    --exclude-module sherpa_onnx --exclude-module numpy \
    --exclude-module curl_cffi --exclude-module pynput \
    --exclude-module mss --exclude-module uiautomation \
    --exclude-module comtypes --exclude-module PIL \
    scripts/server_entry.py
  echo "server setup exe: dist/yaah-server-setup.exe"
else
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
    scripts/server_entry.py
  echo "server: dist/yaah-server"
fi

# PyInstaller leaves the .spec in the CWD (repo root) — it must not ship
# as a release asset (the upload glob catches yaah-server*).
rm -f yaah-server.spec yaah-server-setup.spec
