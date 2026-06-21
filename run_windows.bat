@echo off
:: ==============================================================================
:: dcBot Windows Startup Script
:: Starts both the PO Token server and the Discord bot concurrently.
:: ==============================================================================

echo ====================================================
echo Starting dcBot and POT Server on Windows...
echo ====================================================

:: 1. Launch POT Server in a separate Command Prompt window
echo --> Launching POT Token Server in background...
if exist ..\bgutil-ytdlp-pot-provider\server\build\main.js (
    start "dcBot - POT Server" cmd /k "node ..\bgutil-ytdlp-pot-provider\server\build\main.js --port 4416"
) else (
    echo Error: POT Server build/main.js not found. Please run setup_windows.bat first.
    pause
    exit /b 1
)

:: 2. Launch the Discord bot in the current window
echo --> Starting Discord bot...
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe dc.py
) else (
    echo Error: Python virtual environment not found. Please run setup_windows.bat first.
    pause
    exit /b 1
)
