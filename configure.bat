@echo off
REM LAUNCHER 40: "Which tools to offer" "Какие инструменты предлагать"
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "UV=%SCRIPT_DIR%uv.exe"
call "%SCRIPT_DIR%lang.bat"

set "SUB=comfyui-mcp - which tools to offer"
if /I "%LC%"=="ru" set "SUB=comfyui-mcp - какие инструменты предлагать"
call "%SCRIPT_DIR%logo.bat" "%SUB%"

if exist "%UV%" goto :run
if /I "%LC%"=="ru" echo Не найден uv.exe. Запустите сначала install.bat.
if /I not "%LC%"=="ru" echo uv.exe not found. Run install.bat first.
pause
exit /b 1

:run
REM The window reads the list of tools from the server itself, so a new tool
REM appears in it on its own. It edits one COMFYUI_TOOLS line in .env.
"%UV%" run python -m comfyui_mcp.configure %*
if errorlevel 1 pause
