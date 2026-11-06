@echo off
setlocal
cd /d "%~dp0"
title Spotify pairing helper
set "WORK=%LOCALAPPDATA%\dcbot-soloist"
set "OUT=%~dp0spotify-login.zip"

echo Spotify pairing helper.
echo This pairs Spotify Soloist once on this computer and saves the login as spotify-login.zip.
echo.

if not exist "%WORK%\soloist.exe" (
    echo Downloading Spotify Soloist from Spotify's website. One moment.
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$w='%WORK%'; New-Item -ItemType Directory -Force $w | Out-Null;" ^
      "Invoke-WebRequest 'https://soloist-builds.spotifycdn.com/soloist_release_windows_x86_64.zip' -OutFile \"$w\soloist.zip\";" ^
      "Expand-Archive -Force \"$w\soloist.zip\" \"$w\extract\";" ^
      "Get-ChildItem -Recurse \"$w\extract\" | Where-Object { $_.Name -eq 'soloist.exe' } | Select-Object -First 1 | Copy-Item -Destination $w"
)
if not exist "%WORK%\soloist.exe" (
    echo Could not download Soloist. Check your internet connection and run this again.
    timeout /t 20 >nul
    goto :eof
)

set "KEY="
set /p "KEY=Paste your Spotify Soloist key (starts with spak_) and press Enter: "
if not defined KEY goto :eof

if exist "%WORK%\pair-data" rmdir /s /q "%WORK%\pair-data"
echo.
echo Now open the Spotify app on this computer, or on your phone on the same Wi-Fi.
echo Open the list of devices and choose "Discord Music Bot", then play any song.
echo This window closes the pairing by itself when Spotify connects.
echo.
"%WORK%\soloist.exe" -n "Discord Music Bot" -k "%KEY%" -D "%WORK%\pair-data" --pair
if errorlevel 1 (
    echo Pairing did not finish. Run this again.
    timeout /t 20 >nul
    goto :eof
)

if exist "%OUT%" del "%OUT%"
powershell -NoProfile -Command "Compress-Archive -Force '%WORK%\pair-data\*' '%OUT%'"
echo.
echo Done. Your Spotify login was saved as spotify-login.zip in the bot folder.
timeout /t 15 >nul
endlocal
