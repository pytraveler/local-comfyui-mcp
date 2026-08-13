@echo off
REM LAUNCHER 20: "Bridge node into ComfyUI" "Нода моста в ComfyUI"
REM
REM A .bat over install_node.ps1, because a .ps1 is not something a user can run.
REM Measured on Windows 11: `assoc .ps1` is empty, so there is no association at
REM all and a double-click asks what to open it with; and a stock client refuses
REM to run one anyway under the default Restricted execution policy. Every other
REM user-facing script here is a .bat, and this was the one that was not.
REM
REM -ExecutionPolicy Bypass applies to this one process and changes nothing on the
REM machine, which is the whole point: nobody should have to alter a machine-wide
REM policy to install a node. Arguments pass through, so -ComfyRoot still works.
REM
REM The pause is not decoration: the script prints what to do next -- restart
REM ComfyUI, reload the tab -- and a double-clicked window would close on it.
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install_node.ps1" %*
echo.
pause
