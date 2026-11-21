#!/usr/bin/env bash
# Install (or update) the `agent-bridge` command from this checkout.
# Editable install: after `git pull`, the command uses the new code immediately.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/agent-bridge/venv"
mkdir -p "$BIN"

# Remove a v0.1 symlink to bridge.py if present.
if [ -L "$BIN/agent-bridge" ] && [[ "$(readlink "$BIN/agent-bridge")" == *bridge.py ]]; then
  rm "$BIN/agent-bridge"
fi

if command -v uv >/dev/null 2>&1; then
  echo "Installing with uv…"
  uv tool install --force --editable "$REPO"
else
  echo "Installing into $VENV …"
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || { echo "python3 not found" >&2; exit 1; }
  "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11+ required" >&2; exit 1; }
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet --editable "$REPO"
  ln -sf "$VENV/bin/agent-bridge" "$BIN/agent-bridge"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Note: add $BIN to your PATH." ;;
esac
echo "Done: $(command -v agent-bridge || echo "$BIN/agent-bridge") → $("$BIN/agent-bridge" --version 2>/dev/null || agent-bridge --version)"
