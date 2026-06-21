# dcBot - Discord Music Bot (Self-Hosted)

A high-performance Discord music bot featuring instant streaming, local PO Token (Proof-of-Origin) challenge solving, interactive control buttons, and support for both Linux (VPS) and Windows hosting environments.

---

## 🚀 Features
*   **Instant Playback**: Pre-resolves streams directly for instant voice channel audio.
*   **PO Token Server (BotGuard Bypass)**: Built-in local Proof-of-Origin HTTP server to avoid YouTube anti-bot `403 Forbidden` bans.
*   **JS Challenge Solver**: Integrates Deno-based execution to automatically resolve YouTube's signature challenges in real-time.
*   **Robust Audio Scaling**: Uses perceptually linear scaling curves for clear sound adjustments.
*   **Music Controls**: Rich message embeds with interactive buttons for playback controls.

---

## 🛠️ Prerequisites
*   A Discord Bot Token (from the Discord Developer Portal).
*   A Google Account (Dedicated throwaway account recommended for exporting cookies).

---

## 🐧 Linux Deployment Guide (Ubuntu/VPS)

### 1. Automated Setup
Clone this repository on your Linux server, navigate to the folder, and run the automated installer:
```bash
git clone <your-repo-url> dcBot
cd dcBot
sudo bash setup.sh
```
*Note: `setup.sh` automatically installs Node.js, Deno, Python, FFmpeg, Git, and Supervisor, sets up the virtual environment, compiles the POT server, and creates background service configs.*

### 2. Configuration
*   Create a `.env` file in the bot root directory (`dcBot/.env`):
    ```env
    DISCORD_TOKEN=your_discord_bot_token_here
    ```
*   Follow the **YouTube Cookie Export Guide** below and place the `cookies.txt` file in the bot root directory (`dcBot/cookies.txt`).

### 3. Restart and Manage
Manage your bot and POT server using Supervisor:
```bash
sudo supervisorctl status
sudo supervisorctl restart dcbot
```

---

## 🪟 Windows Deployment Guide

### 1. Prerequisites
Ensure you have the following installed and added to your system environment variables (PATH):
*   [Python 3.10+](https://www.python.org/downloads/)
*   [Node.js (LTS)](https://nodejs.org/)
*   [FFmpeg](https://ffmpeg.org/download.html) (Ensure `ffmpeg` and `ffprobe` executable directories are in your system PATH)

### 2. Automated Setup
1.  Open the bot directory on your PC.
2.  Double-click **`setup_windows.bat`**. This script will:
    *   Set up a Python virtual environment (`.venv`) and install dependencies.
    *   Automatically download and install **Deno** (needed for YouTube challenge solving).
    *   Clone and build the local **POT Token server** backend.

### 3. Configuration
*   Create a `.env` file in the bot root directory (`dcBot/.env`):
    ```env
    DISCORD_TOKEN=your_discord_bot_token_here
    ```
*   Follow the **YouTube Cookie Export Guide** below and place the `cookies.txt` file in the bot root directory (`dcBot/cookies.txt`).

### 4. Running the Bot
*   Double-click **`run_windows.bat`**. This will launch the POT server in a new window and start the Discord bot in the current window. Keep both windows open while using the bot.

---

## 🍪 YouTube Cookie Export Guide (Crucial)
YouTube aggressively blocks requests from datacenter IPs (Linux VPS) or unknown endpoints unless you provide valid login cookies.

1.  Open an **Incognito/Private** browser window.
2.  Go to YouTube and log into your **dedicated bot Google account**.
3.  Play any video for 5–10 seconds.
4.  Using a browser extension (such as **"Get cookies.txt LOCALLY"** for Chrome/Firefox), export your cookies in **Netscape format**.
5.  Save the file as **`cookies.txt`** and upload it to the root of your bot folder (`dcBot/cookies.txt`).
6.  **Important**: Close the incognito browser window immediately. **Do NOT click "Sign Out"** on YouTube, as signing out will instantly invalidate the session ID in your cookies.

---

## 🩺 Troubleshooting Guide

### ❌ Issue: "Sign in to confirm you’re not a bot" or `LOGIN_REQUIRED`
*   **Cause**: The cookies in `cookies.txt` have either expired or been flagged/invalidated by Google because they were detected on a new IP address.
*   **Solution**:
    1.  Repeat the **Cookie Export Guide** steps above to obtain a fresh `cookies.txt`.
    2.  Overwrite the old `cookies.txt` file in your bot directory.
    3.  On Linux, restart the service: `sudo supervisorctl restart dcbot`. On Windows, close and rerun `run_windows.bat`.

### ❌ Issue: Playback Controls (Embed Buttons) do not show up
*   **Cause**: If a video extraction fails during the voice channel play request, the bot exits early before it can build or send the interactive controllers.
*   **Solution**:
    *   Check the error logs. On Linux: `tail -n 50 err.log`. On Windows, check the bot terminal window.
    *   Resolving the cookie issue (above) fixes the extraction error, which will cause the controls to show up automatically.

### ❌ Checking Service Statuses (Linux)
```bash
sudo supervisorctl status
```
*   `dcbot`: The Discord bot process.
*   `bgutil-pot`: The PO Token server process.
