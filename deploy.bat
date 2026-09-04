@echo off
setlocal
cd /d E:\git-project\dcBot
echo Starting incremental dcBot deployment...
echo Only eligible changed source files will be uploaded.
E:\git-project\dcBot\.venv\Scripts\python.exe E:\git-project\dcBot\deploy.py
if errorlevel 1 (
    echo Deployment failed.
    pause
    exit /b 1
)
echo Incremental deployment finished.
pause
