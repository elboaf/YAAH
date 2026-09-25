#!/usr/bin/env bash
# Download and stage the official GitHub CLI release for the current build
# platform. The checksum is checked against the checksum manifest published
# with the same upstream release.
set -euo pipefail
cd "$(dirname "$0")/.."

GH_VERSION="2.101.0"
OS=$(uname -s)
ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "ERROR: GitHub CLI bundle does not support architecture $ARCH"; exit 1 ;;
esac
case "$OS" in
  MINGW*|MSYS*|Windows_NT*|CYGWIN*) PLATFORM="windows_$ARCH"; GH_EXE="gh.exe"; ARCHIVE_EXT=zip ;;
  Linux*) PLATFORM="linux_$ARCH"; GH_EXE=gh; ARCHIVE_EXT=tar.gz ;;
  *) echo "ERROR: GitHub CLI bundle does not support OS $OS"; exit 1 ;;
esac

ARCHIVE="gh_${GH_VERSION}_${PLATFORM}.${ARCHIVE_EXT}"
BASE_URL="https://github.com/cli/cli/releases/download/v${GH_VERSION}"
# Pin the official release checksum manifest digest as well as the version so
# a compromised download endpoint cannot substitute both the archive and hash.
CHECKSUMS_SHA256="f8bbc37fc5568a6a162d1a67b1e9c1afa9139f7b5a46dcde4a57bdaa0db33b60"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    echo "ERROR: sha256sum or shasum is required to verify the GitHub CLI archive" >&2
    return 1
  fi
}

echo "downloading GitHub CLI $GH_VERSION ($PLATFORM)"
curl -fsSL --retry 3 -o "$TMP/$ARCHIVE" "$BASE_URL/$ARCHIVE"
curl -fsSL --retry 3 -o "$TMP/checksums.txt" "$BASE_URL/gh_${GH_VERSION}_checksums.txt"
if [ "$(sha256 "$TMP/checksums.txt")" != "$CHECKSUMS_SHA256" ]; then
  echo "ERROR: SHA-256 mismatch for upstream checksum manifest"
  exit 1
fi
EXPECTED=$(awk -v name="$ARCHIVE" '$2 == name { print $1; exit }' "$TMP/checksums.txt")
if [ -z "$EXPECTED" ]; then
  echo "ERROR: $ARCHIVE is missing from upstream checksum manifest"
  exit 1
fi
if [ "$(sha256 "$TMP/$ARCHIVE")" != "$EXPECTED" ]; then
  echo "ERROR: SHA-256 mismatch for $ARCHIVE"
  exit 1
fi

tmp_extract="$TMP/extract"
mkdir -p "$tmp_extract" backend/gh/bin
if [ "$ARCHIVE_EXT" = zip ]; then
  unzip -p "$TMP/$ARCHIVE" "bin/$GH_EXE" > "$tmp_extract/$GH_EXE"
  unzip -p "$TMP/$ARCHIVE" LICENSE > "$tmp_extract/LICENSE"
else
  tar -xzf "$TMP/$ARCHIVE" -C "$tmp_extract"
  cp "$tmp_extract/gh_${GH_VERSION}_${PLATFORM}/bin/gh" "$tmp_extract/$GH_EXE"
  cp "$tmp_extract/gh_${GH_VERSION}_${PLATFORM}/LICENSE" "$tmp_extract/LICENSE"
fi
[ -s "$tmp_extract/$GH_EXE" ] || { echo "ERROR: gh executable missing from archive"; exit 1; }
[ -s "$tmp_extract/LICENSE" ] || { echo "ERROR: upstream LICENSE missing from archive"; exit 1; }
cp "$tmp_extract/$GH_EXE" "backend/gh/bin/$GH_EXE"
cp "$tmp_extract/LICENSE" backend/gh/LICENSE
if [ "$GH_EXE" != gh.exe ]; then chmod +x backend/gh/bin/gh; fi

echo "GitHub CLI $GH_VERSION staged and checksum-verified: backend/gh/bin/$GH_EXE"
