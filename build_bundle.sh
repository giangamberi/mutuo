#!/usr/bin/env bash
# Build the onedir bundle and package it for the current platform (Linux or
# macOS). Windows uses build_installer.ps1 instead. PyInstaller cannot
# cross-compile, so this must run on the target platform.
#
#   ./build_bundle.sh            # uses ./.venv if present, else python3
#
# Produces dist/Mutuo-<ver>-<platform>-<arch>.{tar.gz,zip}.
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="${PYTHON:-python3}"
fi

"$PY" -m pip install --quiet -r requirements.txt pyinstaller
"$PY" -m PyInstaller Mutuo.spec --noconfirm --clean

VER="$("$PY" -c 'import version; print(version.__version__)')"
ARCH="$(uname -m)"   # x86_64 | aarch64 (Linux) | x86_64 | arm64 (macOS)

if [ "$(uname -s)" = "Darwin" ]; then
    # onedir on macOS is a .app bundle
    OUT="Mutuo-${VER}-macos-${ARCH}.zip"
    (cd dist && zip -qr "$OUT" Mutuo/Mutuo.app)
else
    OUT="Mutuo-${VER}-linux-${ARCH}.tar.gz"
    tar -czf "dist/$OUT" -C dist Mutuo
fi
echo "bundle ready: dist/$OUT"
