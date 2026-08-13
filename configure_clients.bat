@echo off
REM LAUNCHER 50: "Config for an MCP client" "Конфиг для MCP-клиента"
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "UV=%SCRIPT_DIR%uv.exe"
call "%SCRIPT_DIR%lang.bat"

set "SUB=comfyui-mcp - connecting a client"
if /I "%LC%"=="ru" set "SUB=comfyui-mcp - подключение к клиенту"
call "%SCRIPT_DIR%logo.bat" "%SUB%"

if exist "%UV%" goto :run
if /I "%LC%"=="ru" echo Не найден uv.exe. Запустите сначала install.bat.
if /I not "%LC%"=="ru" echo uv.exe not found. Run install.bat first.
pause
exit /b 1

:run
REM A config for an MCP client: Claude Code, Cursor, Kilo Code, OpenCode,
REM LM Studio, Cherry Studio, MiMo Code, OpenClaw, Hermes, Codex, llama.cpp.
REM Six shapes over three axes - container, entry form and file format - and
REM every path is an editable guess.
"%UV%" run python -m comfyui_mcp.configure_clients %*
if errorlevel 1 pause
