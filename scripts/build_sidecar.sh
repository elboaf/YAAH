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
  CLI=$(/usr/bin/find /tmp/whisper-build -name "whisper-cli${EXE}" -type f | head -1)
  mkdir -p backend/whisper/bin
  cp "$CLI" backend/whisper/bin/
  # Windows builds link ggml CPU-variant DLLs; ship the whole set.
  case "$(uname -s)" in
    MINGW*|Windows_NT) cp "$(/usr/bin/dirname "$CLI")"/*.dll backend/whisper/bin/ ;;
  esac
fi
if [ ! -f "backend/whisper/models/$WHISPER_MODEL" ]; then
  echo "downloading $WHISPER_MODEL"
  mkdir -p backend/whisper/models
  curl -sL -o "backend/whisper/models/$WHISPER_MODEL" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_MODEL"
fi
echo "whisper: backend/whisper/bin + backend/whisper/models"
