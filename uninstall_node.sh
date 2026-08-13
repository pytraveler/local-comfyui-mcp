#!/usr/bin/env bash
# LAUNCHER 25: "Remove the bridge node" "Убрать ноду моста"

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec bash "$SCRIPT_DIR/install_node.sh" --uninstall "$@"
