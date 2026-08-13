@echo off
chcp 65001 >nul
setlocal

REM One window over the scripts in this folder.
REM
REM No LAUNCHER marker of its own, deliberately: a menu offering to reopen itself
REM is not a menu item, and the discovery in launcher.py needs no exclusion list
REM to leave it out - the absent marker is the whole mechanism.
REM
REM The window is Python and Python here lives in .venv, which is what install
REM builds. So on a fresh clone there is exactly one thing to do first, and this
REM does it rather than failing with a message about a missing interpreter.
REM Everything it starts afterwards opens in a console of its own, so install
REM keeps its `pause` and its closing question and needed no unattended mode.

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "UV=%SCRIPT_DIR%uv.exe"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo No .venv yet - running install.bat first.
    echo.
    call "%SCRIPT_DIR%install.bat"
)

if not exist "%PY%" (
    echo.
    echo Install did not finish: %PY% is still missing.
    pause
    exit /b 1
)

"%UV%" run python -m comfyui_mcp.launcher %*
if errorlevel 1 pause
