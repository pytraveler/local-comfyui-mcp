@echo off
REM ============================================================
REM  The project's banner, in one place.
REM
REM  Called rather than pasted into each script, for the reason
REM  lang.bat is: five copies of a block whose every |, \ and `
REM  has to survive cmd's parser is five chances to break one of
REM  them, and a broken one still prints - just wrong.
REM
REM  %1, optional: a one-line subtitle, already in the language it
REM  should be in. This file knows nothing about languages.
REM
REM  setlocal, so the colour variables leave no trace in the caller.
REM  The ESC dance is the only way to get a real escape character
REM  into a variable from batch; conhost has interpreted them since
REM  Windows 10.
REM ============================================================
setlocal
for /f %%A in ('echo prompt $E ^| cmd') do set "ESC=%%A"
set "G=%ESC%[92m"
set "Y=%ESC%[93m"
set "R=%ESC%[0m"

echo %G% ==================================================%R%
echo %Y%
echo              _                       _
echo  _ __  _   _^| ^|_ _ __ __ ___   _____^| ^| ___ _ __
echo ^| '_ \^| ^| ^| ^| __^| '__/ _` \ \ / / _ \ ^|/ _ \ '__^|
echo ^| ^|_) ^| ^|_^| ^| ^|_^| ^| ^| (_^| ^|\ V /  __/ ^|  __/ ^|
echo ^| .__/ \__, ^|\__^|_^|  \__,_^| \_/ \___^|_^|\___^|_^|
echo ^|_^|    ^|___/
if not "%~1"=="" echo.
if not "%~1"=="" echo   %~1
echo %R%%G% ==================================================%R%
echo.
