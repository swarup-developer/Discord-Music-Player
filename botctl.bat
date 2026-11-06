@echo off
setlocal
cd /d "%~dp0"
title Music Bot Control

rem ---- Find a working Python (once) -------------------------------------
set "PY="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import paramiko" >nul 2>&1 && set "PY=.venv\Scripts\python.exe"
)
if not defined PY (
    where uv >nul 2>&1 && set "PY=uv run --no-project --quiet --with paramiko python"
)
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo No working Python found. Install Python from https://www.python.org/downloads/
    echo or uv from https://docs.astral.sh/uv/ and run this again.
    pause
    goto :eof
)
if /i not "%PY:~0,6%"=="uv run" (
    %PY% -c "import paramiko" >nul 2>&1 || %PY% -m pip install --user --quiet paramiko
)

rem ---- Command given on the command line: run it once and exit -----------
if not "%~1"=="" (
    %PY% botctl.py %*
    goto :eof
)

rem ---- Menu ----------------------------------------------------------------
:menu
echo.
echo ===============================
echo   Music Bot Control
echo ===============================
echo   1. Start bot
echo   2. Stop bot (everything)
echo   3. Restart bot
echo   4. Status
echo   5. Logs
echo   6. Change server details
echo   7. Exit
echo.
set "CH="
set "ACT="
set /p "CH=Type a number and press Enter: "

if "%CH%"=="1" set "ACT=start"
if "%CH%"=="2" set "ACT=stop"
if "%CH%"=="3" set "ACT=restart"
if "%CH%"=="4" set "ACT=status"
if "%CH%"=="5" set "ACT=logs"
if "%CH%"=="6" set "ACT=setup"
if "%CH%"=="7" goto :eof

if not defined ACT (
    echo That is not a menu number. Please type 1 to 7.
    goto :menu
)

echo.
%PY% botctl.py %ACT%
echo.
echo Done. Press any key to go back to the menu.
pause >nul
set "ACT="
goto :menu
