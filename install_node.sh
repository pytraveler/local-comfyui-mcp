#!/usr/bin/env bash
# LAUNCHER 20: "Bridge node into ComfyUI" "Нода моста в ComfyUI"
# Installs the bridge node into ComfyUI. The Unix half of install_node.ps1.
#
# A symbolic link rather than a copy, so edits to comfy_node/ take effect on
# the next ComfyUI restart with nothing to re-run and no second copy to drift.
# Where the .ps1 makes a directory junction - Windows' only unprivileged
# equivalent - this makes an ordinary symlink.
#
#   ./install_node.sh                    # install (or repair) the link
#   ./install_node.sh --uninstall        # remove it
#   ./install_node.sh --comfy-root PATH  # when it is not in .env
#
# ComfyUI must be restarted afterwards: custom nodes are imported once at startup.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOURCE="$SCRIPT_DIR/comfy_node/comfyui_mcp_bridge"

usage() {
    cat <<'EOF'
  ./install_node.sh                    install (or repair) the link
  ./install_node.sh --uninstall        remove it
  ./install_node.sh --comfy-root PATH  when COMFYUI_ROOT is not in .env
EOF
}

COMFY_ROOT="${COMFYUI_ROOT:-}"
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) UNINSTALL=1 ;;
        --comfy-root) shift; COMFY_ROOT="${1:-}" ;;
        --comfy-root=*) COMFY_ROOT="${1#*=}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

bash "$SCRIPT_DIR/logo.sh" "$(
    if [ "$UNINSTALL" = 1 ]; then
        echo "comfyui-mcp - removing the bridge node"
    else
        echo "comfyui-mcp - installing the bridge node"
    fi
)"

if [ -z "$COMFY_ROOT" ] && [ -f "$SCRIPT_DIR/.env" ]; then
    COMFY_ROOT=$(sed -n 's/^[[:space:]]*COMFYUI_ROOT[[:space:]]*=[[:space:]]*//p' "$SCRIPT_DIR/.env" | tail -n 1)
    COMFY_ROOT="${COMFY_ROOT%$'\r'}"
    COMFY_ROOT="${COMFY_ROOT%\"}"; COMFY_ROOT="${COMFY_ROOT#\"}"
fi
if [ -z "$COMFY_ROOT" ]; then
    echo "COMFYUI_ROOT is not set. Put it in .env, or pass --comfy-root <path>." >&2
    exit 1
fi

CUSTOM_NODES="$COMFY_ROOT/ComfyUI/custom_nodes"
if [ ! -d "$CUSTOM_NODES" ]; then
    echo "No custom_nodes directory under $COMFY_ROOT. Is COMFYUI_ROOT right?" >&2
    exit 1
fi
TARGET="$CUSTOM_NODES/comfyui_mcp_bridge"

if [ "$UNINSTALL" = 1 ]; then
    if [ ! -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
        echo "Not installed: $TARGET"
        exit 0
    fi
    if [ ! -L "$TARGET" ]; then
        echo "$TARGET is a real directory, not a link this script made. Remove it by hand." >&2
        exit 1
    fi
    rm "$TARGET"
    echo "Removed $TARGET"
    echo "Restart ComfyUI to unload it."
    exit 0
fi

if [ -L "$TARGET" ]; then
    if [ "$(cd -P "$TARGET" 2>/dev/null && pwd)" = "$SOURCE" ]; then
        echo "Already installed: $TARGET -> $SOURCE"
        exit 0
    fi
    rm "$TARGET"
elif [ -e "$TARGET" ]; then
    echo "$TARGET already exists and is not a link. Remove it by hand first." >&2
    exit 1
fi

ln -s "$SOURCE" "$TARGET"
echo "Installed $TARGET -> $SOURCE"
echo
echo "Restart ComfyUI, then reload the browser tab."
echo "Check it with the workspace_status tool, or:"
echo "  curl http://127.0.0.1:8188/mcp_bridge/clients"
