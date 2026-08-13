#!/usr/bin/env bash
# LAUNCHER 40: "Which tools to offer" "Какие инструменты предлагать"
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"
. "$SCRIPT_DIR/lang.sh"

SUB="comfyui-mcp - which tools to offer"
if [ "$LC" = ru ]; then SUB="comfyui-mcp - какие инструменты предлагать"; fi
bash "$SCRIPT_DIR/logo.sh" "$SUB"

if [ -x "$SCRIPT_DIR/uv" ]; then
    UV="$SCRIPT_DIR/uv"
elif command -v uv >/dev/null 2>&1; then
    UV=uv
else
    if [ "$LC" = ru ]; then
        echo "Не найден uv. Запустите сначала ./install.sh."
    else
        echo "uv not found. Run ./install.sh first."
    fi
    exit 1
fi

"$UV" run python -m comfyui_mcp.configure "$@"
