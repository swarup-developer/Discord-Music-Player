@echo off
:: ==============================================================================
:: dcBot Windows Environment Setup Script
:: ==============================================================================

echo ====================================================
echo Starting dcBot Windows Environment Setup
echo ====================================================

:: 1. Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in PATH. Please install Python 3.10+ from python.org.
    pause
    exit /b 1
)

:: 2. Check for Node.js
node -v >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Node.js is not installed or not in PATH. Please install Node.js from nodejs.org.
    pause
    exit /b 1
)

:: 3. Check for Deno
deno --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Warning: Deno is not installed. YouTube signature challenges require Deno.
    echo Installing Deno now via PowerShell...
    powershell -Command "irm https://deno.land/install.ps1 | iex"
    echo Please restart your terminal/PC after setup to apply Deno path changes.
) else (
    echo Deno is already installed.
)

:: 4. Set up Python virtual environment and dependencies
echo --> Setting up Python virtual environment...
if not exist .venv (
    python -m venv .venv
)

echo --> Upgrading pip and installing python packages...
.venv\Scripts\python -m pip install --upgrade pip setuptools wheel
.venv\Scripts\pip install ^
    "discord.py[voice]>=2.7.1" ^
    "python-dotenv>=0.9.9" ^
    "httpx>=0.28.1" ^
    "websockets>=12.0" ^
    "pycryptodome>=3.23.0" ^
    "pynacl>=1.5.0" ^
    "paramiko>=5.0.0" ^
    "yt-dlp>=2026.6.9" ^
    "bgutil-ytdlp-pot-provider>=1.3.1"

:: 5. Set up POT Server
echo --> Deploying POT Server (bgutil-ytdlp-pot-provider)...
cd ..
if not exist bgutil-ytdlp-pot-provider (
    git clone https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git
)

cd bgutil-ytdlp-pot-provider\server
echo --> Installing Node dependencies...
call npm install
echo --> Building server...
call npm run build

cd %~dp0

echo ====================================================
echo Setup complete!
echo Next Steps:
echo 1. Create a '.env' file in this folder with your DISCORD_TOKEN.
echo 2. Export cookies.txt into this folder.
echo 3. Run 'run_windows.bat' to start the bot and POT server.
echo ====================================================
pause
