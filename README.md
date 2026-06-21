# Discord Music Player

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![Discord.py](https://img.shields.io/badge/discord.py-v2.7.1-orange.svg)](https://discordpy.readthedocs.io/en/stable/)
[![Package Manager](https://img.shields.io/badge/managed%20with-uv-purple.svg)](https://github.com/astral-sh/uv)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A high-performance, enterprise-grade, self-hosted Discord music bot. It features instant audio streaming, interactive control panels, real-time audio filters, and automated YouTube BotGuard bypass protocols. Designed for stability, speed, and ease of deployment.

---

## ⚖️ Legal Disclaimer
**This project is an independent self-hosted utility developed solely for educational and personal use. We are NOT associated, authorized, endorsed, or officially connected with Google LLC (YouTube), JioSaavn, or any of their subsidiaries or affiliates. All product names, logos, and brands are the property of their respective owners. Users are responsible for complying with the Terms of Service of all third-party platforms when deploying this bot.**

---

## 🚀 Key Features
*   **Instant Audio Playback**: Pre-resolves streams directly to bypass typical Discord voice buffering and connection delays.
*   **PO Token Server (BotGuard Bypass)**: Built-in local Proof-of-Origin HTTP server to avoid YouTube anti-bot `403 Forbidden` rate-limits.
*   **JS Challenge Solver**: Seamless Deno-based runtime integration to solve YouTube's signature challenges in real-time.
*   **Modular Audio Pipeline**: Implements perceptually linear quadratic volume scaling and advanced FFmpeg Soxr audio filters.
*   **Interactive Control Board**: Rich embeds with interactive buttons for real-time player control (play, pause, skip, filters, volume, and queue paging).

---

## 📦 Project Management with `uv`

This project uses **`uv`** (by Astral) for package resolution and virtual environment management.

### What is `uv`?
`uv` is an extremely fast Python package installer and resolver written in Rust. It serves as a modern replacement for `pip`, `pip-tools`, and `virtualenv`, offering performance improvements that are often 10x–100x faster than traditional tools.

### How it is configured in this bot:
1.  **`pyproject.toml`**: Declares the project metadata, Python compatibility constraints, and dependencies (like `discord.py`, `yt-dlp`, and `paramiko`) in a modern standardized format.
2.  **`uv.lock`**: A cross-platform lockfile that locks the exact versions of all direct and transitive dependencies. This guarantees deterministic, reproducible installations across development, staging, and production environments.

### Developer Commands:
If you have `uv` installed, you can set up the project locally in milliseconds:
```bash
# Install dependencies and sync virtual environment
uv sync

# Run the bot within the managed environment
uv run dc.py

# Add a new dependency to pyproject.toml and sync lockfile
uv add name-of-package
```

---

## 📖 Introduction for Beginners

### What is Discord?
[Discord](https://discord.com) is a free voice, video, and text communication platform used by communities, gamers, and developers to talk and hang out.

### What is a Discord Bot?
A Discord Bot is a virtual user that lives inside your server. It listens for commands and performs automated tasks, such as joining a voice channel to play music.

---

## 🛠️ Step 1: Create Your Discord Bot & Get a Token

To host the bot, you must register it as an application on Discord and obtain a secret **Bot Token**.

### 1. Register the Bot Application:
1.  Navigate to the [Discord Developer Portal](https://discord.com/developers/applications).
2.  Click the **New Application** button in the top right.
3.  Name your application (e.g., *Discord Music Player*) and click **Create**.
4.  In the left sidebar, click on **Bot**, then click **Add Bot** and confirm.

### 2. Enable Privileged Gateway Intents (Required):
1.  Scroll down to the **Privileged Gateway Intents** section on the **Bot** page.
2.  Turn **ON** the following toggles:
    *   **Presence Intent**
    *   **Server Members Intent**
    *   **Message Content Intent** (Crucial for reading music commands).
3.  Click **Save Changes**.

### 3. Retrieve Your Token:
1.  In the **Token** section at the top of the **Bot** page, click **Reset Token**.
2.  Copy and save the long token string. Keep this secret!

### 4. Invite the Bot to Your Server:
1.  In the Developer Portal sidebar, click **OAuth2**, then select **URL Generator**.
2.  Under **Scopes**, select: `bot` and `applications.commands`.
3.  Under **Bot Permissions**, select:
    *   **Send Messages**
    *   **Embed Links**
    *   **Connect**
    *   **Speak**
    *   **Use Voice Activity**
4.  Copy the generated URL at the bottom of the page, paste it into your browser, and authorize it for your server.

---

## 🐧 Linux Deployment Guide (Ubuntu/VPS)

If you are hosting your bot on a remote Linux server (such as an Ubuntu VPS on AWS, Oracle Cloud, or DigitalOcean):

### 1. Run the Installer Script
Execute the installer to automatically configure Node.js, Deno, Python, FFmpeg, Git, and Supervisor:
```bash
git clone <your-repo-url> DiscordMusicPlayer
cd DiscordMusicPlayer
sudo bash setup.sh
```

### 2. Configure Environment Secrets
*   Create a `.env` file in the root directory:
    ```env
    DISCORD_TOKEN=your_copied_bot_token_here
    ```
*   Follow the **YouTube Cookie Guide** below and place the `cookies.txt` file in the root directory.

### 3. Manage Background Services
Supervisor runs the bot and the PO Token server in the background:
```bash
# Check statuses
sudo supervisorctl status

# Restart the bot
sudo supervisorctl restart dcbot
```

---

## 🪟 Windows Deployment Guide (Local Host)

### 1. Install Prerequisites
Ensure the following are installed and added to your system environment variables (PATH):
*   [Python 3.10+](https://www.python.org/downloads/) (Check **"Add Python to PATH"**).
*   [Node.js (LTS)](https://nodejs.org/)
*   [FFmpeg](https://ffmpeg.org/download.html) (Ensure `ffmpeg` and `ffprobe` executable directories are in your system PATH).

### 2. Run the Installer
1.  Open the bot folder.
2.  Double-click **`setup_windows.bat`**. This sets up the virtual environment, installs dependencies, downloads Deno, and builds the POT server.

### 3. Configuration
*   Create a `.env` file in the root directory:
    ```env
    DISCORD_TOKEN=your_copied_bot_token_here
    ```
*   Follow the **YouTube Cookie Guide** below and place the `cookies.txt` file in the root directory.

### 4. Start the Application
*   Double-click **`run_windows.bat`**. This launches both the POT server and the Discord bot concurrently. Keep both command prompt windows open.

---

## 🍪 YouTube Cookie Guide (Bypass Verification Blocks)
YouTube blocks requests from datacenter IPs unless valid login cookies are provided.

1.  Open an **Incognito/Private** window in your browser.
2.  Log into a **dedicated/throwaway Google Account** on YouTube (do not use your main personal account).
3.  Play any video for 5–10 seconds.
4.  Export the cookies in **Netscape format** using a browser extension (such as **"Get cookies.txt LOCALLY"**).
5.  Save the file as **`cookies.txt`** and place it in the bot root directory (`cookies.txt`).
6.  **Important**: Close the incognito window immediately. **Do NOT click "Sign Out"** on YouTube, or your cookies will instantly become invalid.

---

## 🎵 JioSaavn Integration
JioSaavn integration works out of the box using public API queries and HTML scrapers.
*   **Zero Configuration**: JioSaavn requires no API keys or login accounts.
*   **Switching Providers**: Switch your default search provider dynamically using the `/provider` command.

---

## 🎮 Slash Commands
All interactions are handled via Discord's Slash Commands:

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `/play` | `song` (text / search query) | Plays a song immediately or queues it (YouTube / JioSaavn search). |
| `/url` | `url` (link) | Plays a YouTube or direct audio stream URL. |
| `/search` | `query` (text) | Searches for songs on the active provider. |
| `/provider` | None | Opens the interactive button menu to switch the default music provider. |
| `/queue` | None | Shows the current music queue with pagination. |
| `/lyrics` | `song` (optional text) | Looks up lyrics for the current song or a searched song. |
| `/volume` | `level` (integer 0-100) | Adjusts the playback volume for this server. |
| `/skip` | None | Skips the currently playing track. |
| `/pause` | None | Pauses the music playback. |
| `/resume` | None | Resumes the paused music playback. |
| `/stop` | None | Stops the playback and clears the queue. |
| `/join` | None | Forces the bot to join your active voice channel. |
| `/go` | None | Tells the bot to leave the voice channel. |
| `/bassboost`| None | Toggles the bass boost audio filter. |
| `/nightcore`| None | Toggles the nightcore audio filter (pitch up & speed up). |
| `/diagnose` | None | Diagnoses voice connection and latency issues. |
| `/voicecheck`| None | Verifies current voice connection details. |
| `/help` | None | Shows the categorized help menu. |

---

## 🩺 Troubleshooting Guide

### ❌ Issue: "Sign in to confirm you’re not a bot" or `LOGIN_REQUIRED`
*   **Cause**: The session inside `cookies.txt` has expired or was invalidated by Google due to the location/IP change.
*   **Solution**: Repeat the **YouTube Cookie Guide** steps above to obtain a fresh `cookies.txt` file, replace the old one, and restart the bot.

### ❌ Issue: Playback control buttons are missing
*   **Cause**: If the video link fails to play (due to cookie errors or network blockages), the bot crashes early and cannot show the buttons.
*   **Solution**: Fixing the cookies (above) will make the buttons show up automatically when the music starts playing.

### ❌ Checking Linux Logs
To see what the bot is doing or why it failed:
```bash
tail -n 50 err.log
```
