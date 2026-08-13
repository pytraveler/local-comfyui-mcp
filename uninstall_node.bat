@echo off
REM LAUNCHER 25: "Remove the bridge node" "Убрать ноду моста"
REM
REM A second entry point to install_node.ps1 -Uninstall, not a second copy of it.
REM The part worth being careful about stays in one place: that script refuses to
REM delete `custom_nodes\comfyui_mcp_bridge` when it is a real directory rather
REM than the junction it made, and removes the link without touching the target.
REM
REM It exists because "run the install script with a switch" is not something to
REM ask of somebody who has no reason to own a terminal -- and because a .ps1
REM cannot be double-clicked at all. See install_node.bat for that measurement.
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install_node.ps1" -Uninstall %*
echo.
pause
