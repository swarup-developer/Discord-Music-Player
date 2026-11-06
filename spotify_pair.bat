@echo off
setlocal
cd /d "%~dp0"
title Spotify setup for the music bot
echo Spotify setup for the music bot.
echo.

set "PY="
where uv >nul 2>&1 && set "PY=uv run --no-project --quiet --with paramiko --with zeroconf python"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo No working Python found. Install Python from https://www.python.org/downloads/ and run this again.
    timeout /t 20 >nul
    goto :eof
)
if /i not "%PY:~0,6%"=="uv run" (
    %PY% -c "import paramiko, zeroconf" >nul 2>&1 || %PY% -m pip install --user --quiet paramiko zeroconf
)

if not exist "spotify_pair.py" (
    powershell -NoProfile -Command "Invoke-WebRequest 'https://github.com/swarup-developer/Discord-Music-Player/raw/main/spotify_pair.py' -OutFile 'spotify_pair.py'" >nul 2>&1
)
%PY% spotify_pair.py
timeout /t 30 >nul
endlocal
