#!/usr/bin/env bash
# One window over the scripts in this folder. The twin of launcher.bat; see the
# notes at the top of that file for why it carries no LAUNCHER marker itself and
# why install needed no unattended mode.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"
UV="$SCRIPT_DIR/uv"
PY=".venv/bin/python"

if [ ! -x "$PY" ]; then
    echo "No .venv yet - running install.sh first."
    echo
    bash "$SCRIPT_DIR/install.sh"
fi

if [ ! -x "$PY" ]; then
    echo
    echo "Install did not finish: $PY is still missing."
    exit 1
fi

"$UV" run python -m comfyui_mcp.launcher "$@"
