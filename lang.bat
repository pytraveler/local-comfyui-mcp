@echo off
REM ============================================================
REM  Which language the .bat half speaks. Sets LC to "en" or "ru".
REM
REM  Called rather than copied into each script: four files asking
REM  the same question four slightly different ways is exactly the
REM  drift the rest of this project refuses.
REM
REM  Deliberately no setlocal - the whole point is to set LC in the
REM  caller's environment.
REM
REM  Three sources, in order of how deliberate they are, matching
REM  what i18n.detect() does on the Python side:
REM    1. COMFYUI_LANG in the environment
REM    2. COMFYUI_LANG in .env, which is where the settings windows write it
REM    3. the Windows UI language, from the registry
REM  Anything unrecognised, and anything missing, means English.
REM ============================================================
set "LC="

if defined COMFYUI_LANG set "LC=%COMFYUI_LANG%"
if defined LC goto :lang_clean

if not exist "%~dp0.env" goto :lang_registry
REM eol=# skips the comment lines; delims== splits KEY from value.
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%~dp0.env") do (
    if /I "%%a"=="COMFYUI_LANG" set "LC=%%b"
)
if defined LC goto :lang_clean

:lang_registry
REM LocaleName is "ru-RU", "en-US" and so on. The header line of the
REM query has fewer than three tokens and so sets nothing.
for /f "tokens=3" %%a in ('reg query "HKCU\Control Panel\International" /v LocaleName 2^>nul') do set "LC=%%a"

:lang_clean
if not defined LC goto :lang_en
REM A .env value may carry quotes; a two-character comparison would
REM otherwise see the quote rather than the language.
set LC=%LC:"=%
if not defined LC goto :lang_en
if /I "%LC:~0,2%"=="ru" goto :lang_ru

:lang_en
set "LC=en"
goto :eof

:lang_ru
set "LC=ru"
goto :eof
