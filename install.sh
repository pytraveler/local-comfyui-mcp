#!/usr/bin/env bash
# LAUNCHER 10: "Install: venv, .env, tests" "Установка: venv, .env, тесты"
# ============================================================
#  The Unix half of install.bat, step for step.
#
#  Two things genuinely differ, and each is marked where it happens:
#  the uv asset is chosen by platform rather than being one .zip, and
#  the interpreter lives at .venv/bin/python rather than .venv\Scripts\.
#
#  What is deliberately the same: the step numbering, the idempotence,
#  and the two-language output through `say`.
#
#  No `set -e`, on purpose: several steps are meant to report a failure
#  and carry on - the tests failing does not undo an install that
#  happened. Every command that must succeed is checked where it runs.
# ============================================================
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

. "$SCRIPT_DIR/lang.sh"

say() { if [ "$LC" = ru ]; then echo "$2"; else echo "$1"; fi; }
die() { say "$1" "$2"; exit 1; }

SUB="comfyui-mcp - setting up the venv"
if [ "$LC" = ru ]; then SUB="comfyui-mcp - установка venv-окружения"; fi
bash "$SCRIPT_DIR/logo.sh" "$SUB"

case "$(uname -s)" in
    Linux | Darwin) ;;
    MINGW* | MSYS* | CYGWIN*)
        die "This is Windows - run install.bat instead." \
            "Это Windows - запускайте install.bat." ;;
    *)  die "Unsupported platform: $(uname -s)." \
            "Платформа не поддерживается: $(uname -s)." ;;
esac

UV="$SCRIPT_DIR/uv"
PY=".venv/bin/python"

# ============================================================
#  Step 1: uv
#  It is not committed, so a fresh clone has to fetch it. Unlike the
#  Windows build there is one asset per platform, so the triple has to
#  be worked out rather than hardcoded.
# ============================================================
uv_asset() {
    local os arch
    case "$(uname -s)" in
        Linux) os=unknown-linux-gnu ;;
        Darwin) os=apple-darwin ;;
    esac
    if [ "$os" = unknown-linux-gnu ] && ldd --version 2>&1 | grep -qi musl; then
        os=unknown-linux-musl
    fi
    case "$(uname -m)" in
        x86_64|amd64) arch=x86_64 ;;
        aarch64|arm64) arch=aarch64 ;;
        *)  die "Unsupported architecture: $(uname -m)." \
                "Архитектура не поддерживается: $(uname -m)." ;;
    esac
    printf 'uv-%s-%s.tar.gz' "$arch" "$os"
}

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -q "$1" -O "$2"
    else
        die "Neither curl nor wget is installed." "Нет ни curl, ни wget."
    fi
}

if [ -x "$UV" ]; then
    say "[1/5] uv is already here" "[1/5] uv уже на месте"
else
    say "[1/5] Downloading uv..." "[1/5] Скачиваю uv..."
    ASSET=$(uv_asset) || exit 1
    mkdir -p downloads
    if ! fetch "https://github.com/astral-sh/uv/releases/latest/download/$ASSET" downloads/uv.tar.gz; then
        rm -rf downloads
        die "  ERROR: could not download uv. Check your internet access." \
            "  ОШИБКА: не удалось скачать uv. Проверьте доступ в интернет."
    fi
    mkdir -p downloads/uv_tmp
    tar -xzf downloads/uv.tar.gz -C downloads/uv_tmp
    found=$(find downloads/uv_tmp -type f -name uv | head -n 1)
    if [ -n "$found" ]; then
        mv "$found" "$UV"
        chmod +x "$UV"
    fi
    rm -rf downloads
    [ -x "$UV" ] || die \
        "  ERROR: the uv archive did not contain a uv binary." \
        "  ОШИБКА: в архиве uv не оказалось самого uv."
    say "  [OK] uv downloaded" "  [OK] uv загружен"
fi

# ============================================================
#  Step 2: the environment
#  uv sync reads pyproject.toml and uv.lock: it creates a .venv of the
#  right Python version, installs the dependencies and the package
#  itself. Running it again is harmless.
# ============================================================
echo
say "[2/5] Building the .venv..." "[2/5] Собираю окружение .venv..."
if ! "$UV" sync; then
    die "  ERROR: uv sync failed. See the output above." \
        "  ОШИБКА: uv sync завершился неудачно. Смотрите вывод выше."
fi
if [ ! -x "$PY" ]; then
    die "  ERROR: uv finished, but $PY is not there." \
        "  ОШИБКА: uv отработал, но $PY не найден."
fi
say "  [OK] dependencies installed" "  [OK] зависимости установлены"

# ============================================================
#  Step 3: .env
#  The server reads its settings from a file rather than from shell
#  variables: an MCP client launches a stdio server with a whitelisted
#  environment. The template is seeded in the reader's own language.
# ============================================================
echo
say "[3/5] Checking .env..." "[3/5] Проверяю .env..."
TEMPLATE=".env.example"
if [ "$LC" = ru ] && [ -f ".env.example.ru" ]; then TEMPLATE=".env.example.ru"; fi
if [ -f ".env" ]; then
    say "  [OK] .env is already there - leaving it alone" "  [OK] .env уже есть - оставляю как есть"
else
    cp "$TEMPLATE" .env
    say "  [i] .env created from $TEMPLATE" "  [i] .env создан из $TEMPLATE"
fi

# ============================================================
#  Step 4: checking the server
#  Importing also runs the configuration load, so a mistake in .env
#  surfaces here rather than at the first call from a client.
# ============================================================
echo
say "[4/5] Checking the server..." "[4/5] Проверяю сервер..."
ROOT=$("$PY" -c "import comfyui_mcp.server as s; print(s.CFG.comfy_root)") || ROOT=""
if [ -z "$ROOT" ]; then
    die "  ERROR: comfyui_mcp.server does not import. See the output above." \
        "  ОШИБКА: comfyui_mcp.server не импортируется. Смотрите вывод выше."
fi
say "  [OK] the comfyui_mcp.server module loads" "  [OK] модуль comfyui_mcp.server загружается"
if [ -d "$ROOT/ComfyUI" ]; then
    say "  [OK] ComfyUI found: $ROOT" "  [OK] ComfyUI найден: $ROOT"
else
    echo "  [!] COMFYUI_ROOT=$ROOT"
    say "      There is no ComfyUI folder inside it - without one, comfy_start" \
        "      Папки ComfyUI внутри нет - без неё comfy_start и"
    say "      and comfy_status will not work. Opening the settings window:" \
        "      comfy_status работать не будут. Открываю окно настройки:"
    say "      point it at the root of the install." \
        "      укажите в нём корень установки."
    SETUP_SHOWN=1
    "$UV" run python -m comfyui_mcp.configure_comfy
fi

# ============================================================
#  Step 5: the tests
#  The whole suite is offline; ComfyUI is not needed for it.
#
#  Through -m pytest rather than `uv run pytest`: the second form fails
#  right after the project is rebuilt with 'uv trampoline failed to
#  canonicalize script path'.
# ============================================================
echo
say "[5/5] Running the tests..." "[5/5] Прогоняю тесты..."
if "$UV" run python -m pytest -q; then
    say "  [OK] the tests passed" "  [OK] тесты прошли"
else
    say "  [!] The tests did not pass - it installed, but something is wrong." \
        "  [!] Тесты не прошли - установка состоялась, но что-то не так."
fi

echo
echo "========================================"
say "     Installation finished" "     Установка завершена"
echo
say "  ./configure_comfy.sh   - where ComfyUI is: folder, port, launch script" \
    "  ./configure_comfy.sh   - где ComfyUI: папка, порт, скрипт запуска"
say "  ./configure.sh         - which tools to offer (all of them by default)" \
    "  ./configure.sh         - какие инструменты предлагать (по умолчанию все)"
say "  ./configure_clients.sh - a config for a client: Claude Code, Cursor, Kilo," \
    "  ./configure_clients.sh - конфиг для клиента: Claude Code, Cursor, Kilo,"
echo "                           OpenCode, LM Studio, Cherry Studio, MiMo Code,"
echo "                           OpenClaw, Hermes, Codex, llama.cpp"
say "  ./install_node.sh      - the bridge node: reaches the workflow open in the browser" \
    "  ./install_node.sh      - нода моста: доступ к воркфлоу, открытому в браузере"
echo "========================================"
echo

if [ -z "${SETUP_SHOWN:-}" ] && [ -t 0 ]; then
    if [ "$LC" = ru ]; then
        read -r -p "Открыть настройку ComfyUI (configure_comfy.sh)? [y/N] " answer
    else
        read -r -p "Open the ComfyUI setup (configure_comfy.sh)? [y/N] " answer
    fi
    case "$answer" in
        [Yy]*) echo; "$UV" run python -m comfyui_mcp.configure_comfy ;;
    esac
fi

# ============================================================
#  The bridge node, offered rather than installed.
#  It goes into somebody else's ComfyUI, and a part of the world does not want
#  other people's nodes in custom_nodes - so it is a question, not a step. But
#  it is asked, because without it half of this server answers bridge_missing
#  and nothing says why.
#
#  Asked only when it could actually work: the root has to resolve and hold
#  custom_nodes, and an existing link means there is nothing to ask about. The
#  root is read again rather than reused - configure_comfy may have changed it.
#  `[ -t 0 ]` for the reason above: piped into a shell there is nobody to ask.
# ============================================================
ROOT=$("$PY" -c "import comfyui_mcp.server as s; print(s.CFG.comfy_root)" 2>/dev/null) || ROOT=""
if [ -n "$ROOT" ] && [ -d "$ROOT/ComfyUI/custom_nodes" ] && [ -t 0 ]; then
    if [ -e "$ROOT/ComfyUI/custom_nodes/comfyui_mcp_bridge" ]; then
        say "  [OK] the bridge node is already linked in" "  [OK] нода моста уже подключена"
    else
        echo
        say "The bridge node lets the server read and edit the workflow open in the" \
            "Нода моста даёт серверу читать и править воркфлоу, открытый в браузере,"
        say "browser, unsaved changes and all. Without it everything else still works." \
            "вместе с несохранёнными правками. Без неё всё остальное работает."
        say "A symlink into custom_nodes, no copy, removable with ./uninstall_node.sh." \
            "Это симлинк в custom_nodes, не копия; снимается через ./uninstall_node.sh."
        say "ComfyUI has to be restarted afterwards." \
            "После установки нужно перезапустить ComfyUI."
        if [ "$LC" = ru ]; then
            read -r -p "Установить ноду моста сейчас? [y/N] " answer
        else
            read -r -p "Install the bridge node now? [y/N] " answer
        fi
        case "$answer" in
            [Yy]*) echo; bash "$SCRIPT_DIR/install_node.sh" ;;
        esac
    fi
fi
