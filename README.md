# Discord Music Player

Hi! This is a simple, fast, and self-hosted Discord music bot. I built this to play music instantly in your voice channels with interactive buttons, audio filters (like bassboost and nightcore), and a built-in solution to bypass YouTube's bot verification blocks. It works great on both Linux (VPS) and Windows.

---

## Disclaimer
This is a personal, open-source project. I have no official association with Google (YouTube), JioSaavn, or any other music service. This bot is intended for personal and educational use only, so please use it responsibly.

---

## 🚀 Key Features
*   **Instant Playback**: Plays music in your voice channel immediately without buffering lags.
*   **YouTube Bypass**: Integrates a local PO Token server to help prevent YouTube from blocking your server's IP address.
*   **Challenge Solver**: Automatically solves YouTube's signature challenges in the background using Deno.
*   **Interactive Control Board**: Sends a clean message with play, pause, skip, filter, and volume buttons so you don't have to type commands every time.
*   **Fast Dependencies**: Managed with uv for lightning-fast installation and updates.
*   **Startup Sound**: Plays a customizable audio file when the bot joins a voice channel.

### 🎵 Startup Sound
You can set up a custom sound to play immediately whenever the bot joins a voice channel:
1. Create a folder named `audio` in the root of the project (if it doesn't already exist).
2. Place an audio file named `audio.mp3` inside that folder.
3. The bot will automatically play this sound upon joining a voice channel.

*Note: Any `.mp3` files placed in the `audio/` directory are ignored by git to keep your private startup sound private when pushing code to a public repository.*

---

## 📦 What is uv and how is it used here?

If you look at the files, you'll see a pyproject.toml and a uv.lock. This project uses uv (a super fast Python tool written in Rust) to manage packages.

### Why uv?
*   Instead of waiting minutes for pip to install dependencies, uv installs everything in a few milliseconds.
*   The uv.lock file makes sure that everyone who hosts this bot gets the exact same package versions, preventing random crashes.

### How to use it locally (optional):
If you have uv installed on your computer, you can run:
*   "uv sync" to set up your virtual environment and install packages.
*   "uv run dc.py" to start the bot.

---

## 📖 Discord Setup (For absolute beginners)

If you've never created a Discord bot before, don't worry! Here is how to set it up:

### 1. Register Your Bot:
1.  Go to the Discord Developer Portal website:<br>[Discord Developer Portal](https://discord.com/developers/applications)<br>
2.  Click "New Application" in the top right.
3.  Name it (for example, My Music Player) and click "Create".
4.  In the left sidebar, click "Bot", then click "Add Bot" and confirm.

### 2. Enable Gateway Intents:
Scroll down on the Bot page to the "Privileged Gateway Intents" section. Turn ON these three toggles:
*   Presence Intent
*   Server Members Intent
*   Message Content Intent (This lets the bot read your chat commands).
Click "Save Changes".

### 3. Copy the Token:
At the top of the Bot page, click "Reset Token" and copy the long code. This is your bot's password. Keep it safe and never share it.

### 4. Invite the Bot:
1.  In the left sidebar, click "OAuth2", then select "URL Generator".
2.  Under Scopes, check "bot" and "applications.commands".
3.  Under Bot Permissions, check: Send Messages, Embed Links, Connect, Speak, and Use Voice Activity.
4.  Copy the URL generated at the bottom, paste it into your browser, and add the bot to your server.

---

## 🐧 Hosting on Linux (VPS / Ubuntu)

If you have a remote server (like Ubuntu on Oracle Cloud, AWS, or DigitalOcean):

### 1. Run the Installer:
This script installs all system files (Python, Node.js, Deno, FFmpeg, Git, and Supervisor) and compiles the local token server:
```bash
git clone https://github.com/swarup-developer/Discord-Music-Player.git
cd Discord-Music-Player
sudo bash setup.sh
```

### 2. Add Secrets & Cookies:
*   Create a .env file in the folder and add your token:
    ```env
    DISCORD_TOKEN=your_token_here
    ```
*   Follow the YouTube Cookie Guide below to create a cookies.txt and place it in this folder.

### 3. Run:
Supervisor manages the bot in the background:
*   "sudo supervisorctl status" - Check if it's running.
*   "sudo supervisorctl restart dcbot" - Restart the bot.

---

## 🪟 Hosting on Windows (Your own PC)

If you want to run the bot on your Windows computer:

### 1. Install prerequisites:
*   Python 3.10 or newer:<br>[Download Python](https://www.python.org/downloads/)<br>
*   Node.js (LTS version):<br>[Download Node.js](https://nodejs.org/)<br>
*   FFmpeg:<br>[Download FFmpeg](https://ffmpeg.org/download.html)<br>

### 2. Install:
1.  Open the bot folder.
2.  Double-click "setup_windows.bat". This will set up the virtual environment, install packages, and set up Deno.

### 3. Add Secrets & Cookies:
*   Create a .env file in the folder and add your token:
    ```env
    DISCORD_TOKEN=your_token_here
    ```
*   Follow the YouTube Cookie Guide below to create a cookies.txt and place it in this folder.

### 4. Run:
*   Double-click "run_windows.bat" to start the bot and the token helper. Keep both windows open.

---

## 🍪 YouTube Cookie Guide (Avoid Blocks)
YouTube blocks server IPs unless you pass browser cookies to prove you are a real person.

1.  Open an Incognito/Private window in your browser.
2.  Log into a dedicated/throwaway Google Account on YouTube (do not use your personal main account).
3.  Play any video for 10 seconds.
4.  Export the cookies in Netscape format using a browser extension (like "Get cookies.txt LOCALLY").
5.  Save it as "cookies.txt" and put it in the bot folder.
6.  **Important**: Close the incognito window. Do not click "Sign Out" on YouTube, or the cookies will expire immediately.

---

## 🎵 JioSaavn Support
JioSaavn works out of the box and doesn't require any login or API keys. You can change your default music search provider using the "/provider" command.

---

## 🎮 Slash Commands
Type "/" in your Discord server to see the commands:

*   /play [song] - Plays a song by name or queues it.
*   /url [link] - Plays a YouTube or direct audio stream link.
*   /search [query] - Search for songs on YouTube or JioSaavn.
*   /provider - Switches between YouTube and JioSaavn.
*   /queue - Shows what songs are playing next.
*   /lyrics [song] - Looks up lyrics for the current or a searched song.
*   /volume [0-100] - Adjusts the music volume.
*   /skip - Skips the current song.
*   /pause - Pauses playback.
*   /resume - Resumes playback.
*   /stop - Stops playback and clears the queue.
*   /join - Forces the bot to join your voice channel.
*   /go - Tells the bot to leave the voice channel.
*   /bassboost - Toggles the bass boost audio filter.
*   /nightcore - Toggles the nightcore audio filter.
*   /diagnose - Checks voice latency and connection issues.
*   /voicecheck - Verifies current voice status.
*   /help - Shows the help menu.

---

## 💾 Persistent Server Settings

The bot now remembers your server preferences. Any settings you configure on a server (guild) are automatically saved locally to `server_settings.json` and will persist even if the bot is restarted or leaves/rejoins a voice channel:
*   **Volume**: Remembers your preferred music volume level.
*   **Provider**: Remembers your chosen search provider (YouTube or JioSaavn).
*   **Loop Mode**: Remembers if repeat/loop is active.
*   **Audio Effects**: Remembers active filters (like bassboost or nightcore).

---

## 🔄 Playback Fallbacks & YouTube OAuth (2026)

If YouTube changes its verification algorithms or blocks standard `yt-dlp` connections, the bot uses the following strategies to maintain playback without requiring cookies:

### 1. Automatic Proxy Fallbacks (Cobalt & Invidious APIs)
*   **How it works**: If standard `yt-dlp` fails with a `Sign in to confirm you're not a bot` or IP block error, the bot automatically cascades extraction requests through public **Cobalt** and **Invidious** API instances.
*   **Benefit**: This proxies stream extraction away from your server's IP, bypassing local rate limits and cookie requirements seamlessly in the background.

### 2. Built-in YouTube OAuth2 Device Flow (CLI Alternative)
If you want to authenticate the downloader directly using a Google account without cookie files:
*   **How it works**: Modern `yt-dlp` supports built-in OAuth authentication.
*   **How to use**: You can run the following command directly on your host machine to authorize your Google account:
    ```bash
    # Run yt-dlp with the oauth flag to initiate authentication
    ./.venv/bin/yt-dlp --username oauth "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ```
*   **Process**: It will output a verification code (e.g. `XXXX-XXXX`) and a URL (`https://www.google.com/device`). Open the link, sign in (a burner Google account is recommended), and input the code. `yt-dlp` will securely cache the tokens on your host machine, bypassing cookie files completely.

---

## 🩺 Troubleshooting

### ❌ YouTube says "Sign in to confirm you're not a bot" or LOGIN_REQUIRED
*   Your cookies have expired. Follow the YouTube Cookie Guide again to export a fresh cookies.txt file and replace the old one.

### ❌ Playback control buttons are not showing up
*   This happens if the video failed to load or play (usually due to bad cookies or voice channel connection issues). Fixing the cookies will restore the buttons on the next play.

### ❌ Check logs on Linux:
```bash
tail -n 50 err.log
```
