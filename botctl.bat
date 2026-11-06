@echo off
setlocal
cd /d "%~dp0"

rem 1) The project's virtual environment, if it still works and has paramiko.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import paramiko" >nul 2>&1 && (
        ".venv\Scripts\python.exe" botctl.py %*
        goto :end
    )
)

rem 2) uv: runs botctl with its own Python and paramiko, no setup needed.
where uv >nul 2>&1 && (
    uv run --no-project --quiet --with paramiko python botctl.py %*
    goto :end
)

rem 3) Any installed Python (py launcher, then python); installs paramiko once.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo No working Python found. Install Python from https://www.python.org/downloads/
    echo or uv from https://docs.astral.sh/uv/ and run this again.
    goto :end
)
%PY% -c "import paramiko" >nul 2>&1 || %PY% -m pip install --user --quiet paramiko
%PY% botctl.py %*

:end
echo.
pause
endlocal
