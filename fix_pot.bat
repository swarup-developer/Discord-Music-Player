@echo off
setlocal enabledelayedexpansion
:: ==============================================================================
:: dcBot - PO Token provider repair + verification
::
:: Installs the bgutil-ytdlp-pot-provider yt-dlp plugin (now declared in
:: pyproject.toml) and verifies that YouTube extraction can actually reach the
:: POT server. Without this plugin yt-dlp gets no PO token and YouTube playback
:: becomes slow and intermittently silent, with nothing in the log to explain it.
::
:: Delayed expansion (!var! not %var%) is required throughout: %errorlevel%
:: inside a parenthesised block is expanded when the block is parsed, i.e.
:: before the commands in it have run.
:: ==============================================================================

cd /d "%~dp0"
set FAILED=0

echo ====================================================
echo  dcBot POT provider repair
echo ====================================================
echo.

if not exist .venv\Scripts\python.exe (
    echo ERROR: .venv\Scripts\python.exe not found. Run setup_windows.bat first.
    set FAILED=1
    goto report
)

:: --- 1. Sync dependencies from pyproject.toml -------------------------------
where uv >nul 2>&1
if !errorlevel! neq 0 goto pipfallback

echo [1/3] Locking and syncing dependencies with uv...
call uv lock
if !errorlevel! neq 0 set FAILED=1
call uv sync
if !errorlevel! neq 0 set FAILED=1
goto checkplugin

:pipfallback
echo [1/3] uv not found in PATH - installing with pip instead.
.venv\Scripts\python -m pip install "bgutil-ytdlp-pot-provider>=1.3.1"
if !errorlevel! neq 0 set FAILED=1

:checkplugin
echo.

:: --- 2. Confirm yt-dlp can see the plugin -----------------------------------
echo [2/3] Checking that yt-dlp loads the plugin...
.venv\Scripts\python -c "import yt_dlp_plugins.extractor" >nul 2>&1
if !errorlevel! neq 0 (
    echo    MISSING: yt_dlp_plugins.extractor did not import.
    echo    The plugin is not installed in .venv - YouTube playback will stay unreliable.
    set FAILED=1
) else (
    echo    OK: plugin is importable.
)
echo.

:: --- 3. Confirm the POT server is listening ---------------------------------
echo [3/3] Checking for the POT server on port 4416...
.venv\Scripts\python -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(s.connect_ex(('127.0.0.1',4416)))" >nul 2>&1
if !errorlevel! neq 0 (
    echo    NOT RUNNING: nothing is listening on 127.0.0.1:4416.
    echo    Expected if the bot is stopped - run_windows.bat starts it.
) else (
    echo    OK: POT server is listening.
)

:report
echo.
echo ====================================================
if !FAILED! neq 0 (
    echo  RESULT: problems found - see the messages above.
) else (
    echo  RESULT: dependencies synced and plugin verified.
    echo  Next: run run_windows.bat, then confirm the log line
    echo        "PO Token provider plugin loaded".
)
echo ====================================================
endlocal
pause
