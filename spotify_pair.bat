@echo off
setlocal
cd /d "%~dp0"
title Spotify setup for the music bot
set "WORK=%LOCALAPPDATA%\dcbot-soloist"
set "ZIP=%~dp0spotify-login.zip"

echo Spotify setup for the music bot.
echo.

rem ---- 1. Get Spotify Soloist (official, from Spotify's website) -----------
if not exist "%WORK%\soloist.exe" (
    echo Downloading Spotify Soloist from Spotify. One moment.
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$w='%WORK%'; New-Item -ItemType Directory -Force $w | Out-Null;" ^
      "Invoke-WebRequest 'https://soloist-builds.spotifycdn.com/soloist_release_windows_x86_64.zip' -OutFile \"$w\soloist.zip\";" ^
      "Expand-Archive -Force \"$w\soloist.zip\" \"$w\extract\";" ^
      "Get-ChildItem -Recurse \"$w\extract\" | Where-Object { $_.Name -eq 'soloist.exe' } | Select-Object -First 1 | Copy-Item -Destination $w"
)
if not exist "%WORK%\soloist.exe" (
    echo Could not download Soloist. Check your internet and run this again.
    timeout /t 20 >nul
    goto :eof
)

rem ---- 2. The only question: the key -------------------------------------
set "KEY="
set /p "KEY=Paste your Spotify key (starts with spak_) and press Enter: "
if not defined KEY goto :eof

rem ---- 3. Pair on this network --------------------------------------------
if exist "%WORK%\pair-data" rmdir /s /q "%WORK%\pair-data"
echo.
echo Now open Spotify on this computer, or on your phone on the same Wi-Fi.
echo Open the device list, choose "Discord Music Bot", and play any song.
echo If Windows asks to allow Soloist on the network, choose Allow.
echo Waiting for Spotify.
"%WORK%\soloist.exe" -n "Discord Music Bot" -k "%KEY%" -D "%WORK%\pair-data" --pair >nul 2>&1
if errorlevel 1 (
    echo Spotify did not connect. Run this again.
    timeout /t 20 >nul
    goto :eof
)
echo Spotify connected.
if exist "%ZIP%" del "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Force '%WORK%\pair-data\*' '%ZIP%'"

rem ---- 4. Send it to the bot (bot owner: uses botctl.json) ----------------
if not exist "botctl.json" goto :manual
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$c=Get-Content botctl.json -Raw | ConvertFrom-Json; $c.host"`) do set "SRV=%%A"
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$c=Get-Content botctl.json -Raw | ConvertFrom-Json; $c.username"`) do set "USR=%%A"
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$c=Get-Content botctl.json -Raw | ConvertFrom-Json; $c.key_path"`) do set "SSHKEY=%%A"
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$c=Get-Content botctl.json -Raw | ConvertFrom-Json; $c.bot_dir"`) do set "BOTDIR=%%A"
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$c=Get-Content botctl.json -Raw | ConvertFrom-Json; $c.discord_user_id"`) do set "DUID=%%A"
if not defined DUID goto :manual
if not exist "%SSHKEY%" goto :manual
rem Windows SSH refuses a key file that other users can read.
icacls "%SSHKEY%" /inheritance:r /grant:r "%USERNAME%:R" >nul 2>&1
echo Sending the login to the bot.
scp -q -i "%SSHKEY%" -o StrictHostKeyChecking=accept-new -o BatchMode=yes "%ZIP%" "%USR%@%SRV%:/tmp/spotify-login-%DUID%.zip"
if errorlevel 1 goto :manual
echo %KEY%| ssh -i "%SSHKEY%" -o BatchMode=yes "%USR%@%SRV%" "cd '%BOTDIR%' && .venv/bin/python -m bot.spotify_admin import %DUID% /tmp/spotify-login-%DUID%.zip; rm -f /tmp/spotify-login-%DUID%.zip"
if errorlevel 1 (
    echo.
    echo The bot could not use this login. Run this again.
    timeout /t 30 >nul
    goto :eof
)
del "%ZIP%" >nul 2>&1
echo.
echo All done. In Discord choose Spotify in /provider, then use /play.
timeout /t 20 >nul
goto :eof

:manual
echo.
echo Done. In Discord run /spotify login, attach spotify-login.zip from this folder,
echo then press Paste key and paste the same key.
timeout /t 30 >nul
endlocal
