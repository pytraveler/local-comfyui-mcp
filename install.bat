@echo off
REM LAUNCHER 10: "Install: venv, .env, tests" "Установка: venv, .env, тесты"
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
REM English or Russian, decided once and used by every :say below.
call "%SCRIPT_DIR%lang.bat"

set "SUB=comfyui-mcp - setting up the venv"
if /I "%LC%"=="ru" set "SUB=comfyui-mcp - установка venv-окружения"
call "%SCRIPT_DIR%logo.bat" "%SUB%"

REM Full path to uv: cmd does not always agree to look for it in the current
REM folder (NoDefaultCurrentDirectoryInExePath). PY stays relative - there was
REM a cd above.
set "UV=%SCRIPT_DIR%uv.exe"
set "PY=.venv\Scripts\python.exe"

REM ============================================================
REM  Step 1: uv.exe
REM  It is not committed, so a fresh clone has to fetch it. uv does
REM  everything else itself.
REM ============================================================
if exist "%UV%" goto :uv_ok

call :say "[1/5] Downloading uv..." "[1/5] Скачиваю uv..."
if not exist "downloads" mkdir "downloads"
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile 'downloads\uv.zip'"
powershell -NoProfile -Command "Expand-Archive -Path 'downloads\uv.zip' -DestinationPath 'downloads\uv_tmp' -Force"
if exist "downloads\uv_tmp\uv.exe" copy /y "downloads\uv_tmp\uv.exe" "%UV%" >nul
rmdir /s /q "downloads\uv_tmp" 2>nul
del /f /q "downloads\uv.zip" 2>nul
rmdir "downloads" 2>nul
if not exist "%UV%" (
    call :say "  ERROR: could not download uv. Check your internet access." "  ОШИБКА: не удалось скачать uv. Проверьте доступ в интернет."
    pause
    exit /b 1
)
call :say "  [OK] uv.exe downloaded" "  [OK] uv.exe загружен"
goto :uv_done

:uv_ok
call :say "[1/5] uv.exe is already here" "[1/5] uv.exe уже на месте"

:uv_done

REM ============================================================
REM  Step 2: the environment
REM  uv sync reads pyproject.toml and uv.lock: it creates a .venv of the
REM  right Python version, installs the dependencies and the package
REM  itself. Running it again is harmless - uv brings the environment up
REM  to the state of the lock file.
REM ============================================================
echo.
call :say "[2/5] Building the .venv..." "[2/5] Собираю окружение .venv..."
"%UV%" sync
if errorlevel 1 (
    call :say "  ERROR: uv sync failed. See the output above." "  ОШИБКА: uv sync завершился неудачно. Смотрите вывод выше."
    pause
    exit /b 1
)
if not exist "%PY%" (
    call :say "  ERROR: uv finished, but %PY% is not there." "  ОШИБКА: uv отработал, но %PY% не найден."
    pause
    exit /b 1
)
call :say "  [OK] dependencies installed" "  [OK] зависимости установлены"

REM ============================================================
REM  Step 3: .env
REM  The server reads its settings from a file rather than from shell
REM  variables: an MCP client launches a stdio server with a whitelisted
REM  environment. The template is seeded in the reader's own language.
REM ============================================================
echo.
call :say "[3/5] Checking .env..." "[3/5] Проверяю .env..."
set "TEMPLATE=.env.example"
if /I "%LC%"=="ru" if exist ".env.example.ru" set "TEMPLATE=.env.example.ru"
if exist ".env" (
    call :say "  [OK] .env is already there - leaving it alone" "  [OK] .env уже есть - оставляю как есть"
) else (
    copy /y "%TEMPLATE%" ".env" >nul
    call :say "  [i] .env created from %TEMPLATE%" "  [i] .env создан из %TEMPLATE%"
)

REM ============================================================
REM  Step 4: checking the server
REM  Importing also runs the configuration load, so a mistake in .env
REM  surfaces here rather than at the first call from a client.
REM ============================================================
echo.
call :say "[4/5] Checking the server..." "[4/5] Проверяю сервер..."
set "ROOT="
for /f "delims=" %%i in ('%PY% -c "import comfyui_mcp.server as s; print(s.CFG.comfy_root)"') do set "ROOT=%%i"
if not defined ROOT (
    call :say "  ERROR: comfyui_mcp.server does not import. See the output above." "  ОШИБКА: comfyui_mcp.server не импортируется. Смотрите вывод выше."
    pause
    exit /b 1
)
call :say "  [OK] the comfyui_mcp.server module loads" "  [OK] модуль comfyui_mcp.server загружается"
if exist "%ROOT%\ComfyUI" (
    call :say "  [OK] ComfyUI found: %ROOT%" "  [OK] ComfyUI найден: %ROOT%"
) else (
    echo   [!] COMFYUI_ROOT=%ROOT%
    call :say "      There is no ComfyUI folder inside it - without one, comfy_start" "      Папки ComfyUI внутри нет - без неё comfy_start и"
    call :say "      and comfy_status will not work. Opening the settings window:" "      comfy_status работать не будут. Открываю окно настройки:"
    call :say "      point it at the root of the portable build." "      укажите в нём корень portable-сборки."
    REM  Only in this branch: until the path is given, the install is not
    REM  finished. The "Find" button asks a running ComfyUI for the folder.
    set "SETUP_SHOWN=1"
    "%UV%" run python -m comfyui_mcp.configure_comfy
)

REM ============================================================
REM  Step 5: the tests
REM  The whole suite is offline; ComfyUI is not needed for it.
REM
REM  Through -m pytest rather than `uv run pytest`: the second form fails
REM  right after the project is rebuilt with 'uv trampoline failed to
REM  canonicalize script path' - the trampoline in .venv\Scripts points at
REM  a path that has not been rewritten yet. Running it as a module does
REM  not go near that.
REM ============================================================
echo.
call :say "[5/5] Running the tests..." "[5/5] Прогоняю тесты..."
"%UV%" run python -m pytest -q
if errorlevel 1 (
    call :say "  [!] The tests did not pass - it installed, but something is wrong." "  [!] Тесты не прошли - установка состоялась, но что-то не так."
) else (
    call :say "  [OK] the tests passed" "  [OK] тесты прошли"
)

echo.
echo ========================================
call :say "     Installation finished" "     Установка завершена"
echo.
call :say "  configure_comfy.bat   - where ComfyUI is: folder, port, launch .bat" "  configure_comfy.bat   - где ComfyUI: папка, порт, .bat запуска"
call :say "  configure.bat         - which tools to offer (all of them by default)" "  configure.bat         - какие инструменты предлагать (по умолчанию все)"
call :say "  configure_clients.bat - a config for a client: Claude Code, Cursor, Kilo," "  configure_clients.bat - конфиг для клиента: Claude Code, Cursor, Kilo,"
echo                           OpenCode, LM Studio, Cherry Studio, MiMo Code,
echo                           OpenClaw, Hermes, Codex, llama.cpp
call :say "  install_node.bat      - the bridge node: reaches the workflow open in the browser" "  install_node.bat      - нода моста: доступ к воркфлоу, открытому в браузере"
echo ========================================
echo.

REM  A repeat install is usually exactly when something has moved: another
REM  ComfyUI build, another port, another drive. Hence an offer rather than
REM  silence. If the window already opened at step 4, do not ask twice.
if defined SETUP_SHOWN goto node_offer
set "M_ASK=Open the ComfyUI setup (configure_comfy.bat)? [Y/N] "
if /I "%LC%"=="ru" set "M_ASK=Открыть настройку ComfyUI (configure_comfy.bat)? [Y/N] "
choice /C YN /N /M "%M_ASK%"
if errorlevel 2 goto node_offer
if not errorlevel 1 goto node_offer
echo.
"%UV%" run python -m comfyui_mcp.configure_comfy

REM ============================================================
REM  The bridge node, offered rather than installed.
REM  It goes into somebody else's ComfyUI, and a part of the world does not
REM  want other people's nodes in custom_nodes - so it is a question, not a
REM  step. But it is asked, because without it half of this server answers
REM  bridge_missing and nothing says why.
REM
REM  Asked only when it could actually work: the root has to resolve and hold
REM  custom_nodes, and an existing link means there is nothing to ask about.
REM  The root is read again rather than reused - configure_comfy may have just
REM  changed it.
REM
REM  install_node.ps1 directly rather than install_node.bat: that wrapper ends
REM  in a pause of its own, and one at the end of this script is enough.
REM ============================================================
:node_offer
set "ROOT="
for /f "delims=" %%i in ('%PY% -c "import comfyui_mcp.server as s; print(s.CFG.comfy_root)" 2^>nul') do set "ROOT=%%i"
if not defined ROOT goto done
if not exist "%ROOT%\ComfyUI\custom_nodes" goto done
if exist "%ROOT%\ComfyUI\custom_nodes\comfyui_mcp_bridge" (
    call :say "  [OK] the bridge node is already linked in" "  [OK] нода моста уже подключена"
    goto done
)
echo.
call :say "The bridge node lets the server read and edit the workflow open in the" "Нода моста даёт серверу читать и править воркфлоу, открытый в браузере,"
call :say "browser, unsaved changes and all. Without it everything else still works." "вместе с несохранёнными правками. Без неё всё остальное работает."
call :say "A junction into custom_nodes, no copy, removable with uninstall_node.bat." "Это junction в custom_nodes, не копия; снимается через uninstall_node.bat."
call :say "ComfyUI has to be restarted afterwards." "После установки нужно перезапустить ComfyUI."
set "M_NODE=Install the bridge node now? [Y/N] "
if /I "%LC%"=="ru" set "M_NODE=Установить ноду моста сейчас? [Y/N] "
choice /C YN /N /M "%M_NODE%"
if errorlevel 2 goto done
if not errorlevel 1 goto done
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install_node.ps1"

:done
pause
exit /b 0

REM ============================================================
REM  One line, both languages, at the point that prints it - the same
REM  shape as i18n.Text on the Python side, and for the same reason:
REM  there is no key to go stale between them.
REM
REM  Written with goto rather than `if (...) else (...)` because a
REM  message containing a bracket - "(all of them by default)" - would
REM  close the block early and the rest of the line would run as a
REM  command.
REM ============================================================
:say
if /I "%LC%"=="ru" goto :say_ru
echo %~1
goto :eof
:say_ru
echo %~2
goto :eof
