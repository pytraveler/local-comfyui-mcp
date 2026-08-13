@echo off
REM LAUNCHER 30: "Where ComfyUI is" "Где находится ComfyUI"
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "UV=%SCRIPT_DIR%uv.exe"
call "%SCRIPT_DIR%lang.bat"

set "SUB=comfyui-mcp - where ComfyUI is"
if /I "%LC%"=="ru" set "SUB=comfyui-mcp - где ComfyUI"
call "%SCRIPT_DIR%logo.bat" "%SUB%"

if exist "%UV%" goto :run
if /I "%LC%"=="ru" echo Не найден uv.exe. Запустите сначала install.bat.
if /I not "%LC%"=="ru" echo uv.exe not found. Run install.bat first.
pause
exit /b 1

:run
REM Where ComfyUI is and how to reach it: COMFYUI_ROOT, the port, the launch .bat.
REM The neighbouring configure.bat is about something else: what the server is
REM allowed to do.
"%UV%" run python -m comfyui_mcp.configure_comfy %*
if errorlevel 1 pause
