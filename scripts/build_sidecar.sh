#!/usr/bin/env bash
# Build the FastAPI backend into a standalone PyInstaller executable and
# place it where Tauri's externalBin expects it:
#   src-tauri/binaries/backend-<target-triple>[.exe]
# Used by release.yml; also runnable locally before `tauri dev`/`tauri build`
# (externalBin makes the file mandatory for the Tauri CLI).
set -euo pipefail
cd "$(dirname "$0")/.."

pip install -r requirements.txt -r requirements-build.txt

TRIPLE=$(rustc -vV | sed -n 's/^host: //p')
EXE=""
[ "$(uname -s)" = "MINGW" -o "$(uname -s)" = "Windows_NT" ] && EXE=".exe" || true

pyinstaller --noconfirm --clean --onefile --noconsole \
  --name backend \
  --paths . \
  --collect-all uvicorn --collect-all fastapi \
  --collect-all pydantic --collect-all pydantic_core \
  --collect-all anyio --collect-all aiosqlite \
  --collect-all httpx --collect-all httpcore \
  --collect-all curl_cffi \
  scripts/backend_entry.py

mkdir -p src-tauri/binaries
cp "dist/backend$EXE" "src-tauri/binaries/backend-$TRIPLE$EXE"
echo "sidecar: src-tauri/binaries/backend-$TRIPLE$EXE"
