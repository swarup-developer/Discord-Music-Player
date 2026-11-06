@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe botctl.py %*
) else (
    python botctl.py %*
)
pause
