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

# ---- voice dictation: whisper.cpp CLI + pre-packaged ggml model ----
# Built from source for every target (the release assets carry no macOS
# CLI binary) and staged under backend/whisper/, which tauri.conf.json
# ships as resources so it lands next to the bundled backend/*.py files.
WHISPER_TAG=v1.9.2
WHISPER_MODEL=ggml-base-q5_1.bin
if [ ! -f "backend/whisper/bin/whisper-cli$EXE" ]; then
  echo "building whisper.cpp $WHISPER_TAG for $TRIPLE"
  curl -sL -o /tmp/whisper.tar.gz "https://github.com/ggml-org/whisper.cpp/archive/refs/tags/$WHISPER_TAG.tar.gz"
  rm -rf /tmp/whisper-src /tmp/whisper-build
  mkdir -p /tmp/whisper-src
  tar -xzf /tmp/whisper.tar.gz -C /tmp/whisper-src --strip-components=1
  cmake -S /tmp/whisper-src -B /tmp/whisper-build \
    -DCMAKE_BUILD_TYPE=Release \
    -DWHISPER_BUILD_TESTS=OFF \
    -DWHISPER_BUILD_EXAMPLES=ON \
    -DGGML_OPENMP=OFF
  # Build all (no --target: multi-config MSBuild/Xcode generators can't
  # resolve target vcxproj files from the top dir). Examples=ON is required
  # — whisper-cli IS an example; tests stay off, server defaults off.
  cmake --build /tmp/whisper-build --config Release
  # Multi-config generators (MSVC/Xcode) emit into bin/Release; single-config
  # (Make/Ninja) into bin/. Check both explicitly — a find-based lookup
  # proved flaky under Git Bash on the Windows runner.
  CLI=""
  for c in "/tmp/whisper-build/bin/Release/whisper-cli$EXE" \
           "/tmp/whisper-build/bin/whisper-cli$EXE"; do
    if [ -f "$c" ]; then CLI="$c"; break; fi
  done
  if [ -z "$CLI" ]; then
    echo "ERROR: whisper-cli$EXE not found after build; bin tree:"
    find /tmp/whisper-build -name "whisper-cli*" -print || true
    ls -R /tmp/whisper-build/bin 2>/dev/null || true
    exit 1
  fi
  echo "whisper-cli: $CLI"
  mkdir -p backend/whisper/bin
  cp "$CLI" backend/whisper/bin/
  # Shared ggml DLLs, if this configuration produced any (static builds make
  # none — an empty glob must not kill the script).
  BIN_DIR=$(dirname "$CLI")
  cp "$BIN_DIR"/*.dll backend/whisper/bin/ 2>/dev/null || true
fi
if [ ! -f "backend/whisper/models/$WHISPER_MODEL" ]; then
  echo "downloading $WHISPER_MODEL"
  mkdir -p backend/whisper/models
  curl -sL -o "backend/whisper/models/$WHISPER_MODEL" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_MODEL"
fi
echo "whisper: backend/whisper/bin + backend/whisper/models"
