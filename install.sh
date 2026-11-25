#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/vanphu1201/StaffKit.git"
INSTALL_DIR="${STAFFKIT_INSTALL_DIR:-$HOME/.local/share/staffkit}"

echo "==> Installing StaffKit v3.0.1 into $INSTALL_DIR"

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git is required." >&2
  exit 1
fi

PY_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  echo "ERROR: python3 (>=3.11) is required." >&2
  exit 1
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "==> Updating existing repository..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

if [ ! -d "$INSTALL_DIR/.venv" ]; then
  echo "==> Creating Python virtualenv..."
  "$PY_BIN" -m venv "$INSTALL_DIR/.venv"
fi

echo "==> Installing package dependencies..."
"$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"

echo "==> Registering plugin in ~/.gemini/config/plugins/staffkit..."
"$INSTALL_DIR/.venv/bin/python" -m engine.cli install-plugin

echo "==> Running 0-token self-verification (plugin-eval)..."
"$INSTALL_DIR/.venv/bin/python" -m engine.cli plugin-eval

echo ""
echo "DONE! Reload Antigravity IDE (or toggle StaffKit in Plugins Manager) to start using:"
echo "  /brainstorm-to-product  |  /deep-fix  |  /audit-and-evolve  |  /pr-review  |  /refactor-safe"
