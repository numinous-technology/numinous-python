#!/bin/sh
# Numinous Cloud CLI installer
#   curl -fsSL https://cloud.numinous.technology/install.sh | sh
set -eu

PKG="numinous"
BIN_DIR="${NUMINOUS_BIN:-$HOME/.local/bin}"

say() { printf '\033[1;34mnuminous\033[0m %s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required" >&2; exit 1; }

say "installing $PKG from PyPI"

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$PKG" >/dev/null
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$PKG" >/dev/null
else
  python3 -m pip install --user --upgrade "$PKG" >/dev/null
fi

mkdir -p "$BIN_DIR"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "add $BIN_DIR to your PATH" ;;
esac

say "installed. next:"
say '  export NUMINOUS_API_URL=... NUMINOUS_API_KEY=...'
say '  numinous capacity'
say '  numinous template pack --name build-env --image ubuntu:24.04'
