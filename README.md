# Discord Music Player

A high-performance, self-hosted Discord music bot featuring instant audio streaming, interactive control buttons, real-time audio filters, and built-in YouTube BotGuard bypass solutions. Designed to be easy to host and use for everyone, from beginners to advanced users.

---

## ⚖️ Disclaimer
**This project is a self-hosted tool developed for educational and personal use only. We are NOT associated, authorized, endorsed, or officially connected with Google LLC (YouTube), JioSaavn, or any of their subsidiaries or affiliates. All product and company names, trademarks, and logos are the property of their respective owners. Users are responsible for complying with the terms of service of the respective platforms when deploying this bot.**

---

## 📖 Introduction for Beginners

### What is Discord?
[Discord](https://discord.com) is a free voice, video, and text communication service used by tens of millions of people to talk and hang out with their communities and friends.

### What is a Discord Bot?
A Discord Bot is a virtual assistant or app that lives inside your Discord server. It listens to commands typed by users in chat channels and performs tasks—like joining a voice channel and playing music!

---

## 🛠️ Step 1: Create Your Discord Bot & Get a Token
To host this bot, you must register it as an application on Discord and get a secret **Bot Token** (which acts like a password for your bot).

### How to Create a Bot:
1.  **Open the Developer Portal**: Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2.  **Create a New Application**: 
    *   Click the **New Application** button in the top right.
    *   Give it a name (e.g., *Discord Music Player*) and click **Create**.
3.  **Set Up the Bot**:
    *   In the left sidebar, click on **Bot**.
    *   Click **Add Bot** and confirm.
4.  **Enable Gateway Intents (CRITICAL)**:
    *   Scroll down on the **Bot** page to the **Privileged Gateway Intents** section.
    *   Turn **ON** the following toggles:
        *   **Presence Intent**
        *   **Server Members Intent**
        *   **Message Content Intent** (This allows the bot to read playback commands).
    *   Click **Save Changes**.
5.  **Get Your Bot Token**:
    *   On the same **Bot** page, look for the **Token** section at the top.
    *   Click **Reset Token** and copy the long string of letters and numbers.
    *   *Warning: Keep this token secret! Never share it with anyone, as it controls your bot.*

### How to Invite the Bot to Your Server:
1.  In the left sidebar of the Developer Portal, click **OAuth2**, then select **URL Generator**.
2.  Under **Scopes**, check the box for **bot** and **applications.commands**.
3.  Under **Bot Permissions**, check:
    *   **Send Messages**
    *   **Embed Links**
    *   **Connect** (Voice permission)
    *   **Speak** (Voice permission)
    *   **Use Voice Activity**
4.  Copy the generated URL at the bottom of the page.
5.  Paste this URL into your web browser, select your Discord server, and click **Authorize**.

---

## 🐧 Linux Setup Guide (For VPS Hosting)

If you are hosting your bot on a remote Linux server (like an Ubuntu VPS from Oracle Cloud, AWS, or DigitalOcean):

### 1. Run the Installer
Run the automated script to install Python, Node.js, Deno, Git, FFmpeg, and the background process manager (Supervisor):
```bash
git clone <your-repo-url> DiscordMusicPlayer
cd DiscordMusicPlayer
sudo bash setup.sh
```
*Note: `setup.sh` automatically installs Node.js, Deno, Python, FFmpeg, Git, and Supervisor, sets up the virtual environment, compiles the POT server, and creates background service configs.*

### 2. Configure Settings
*   Create a file named `.env` in the bot directory:
    ```env
    DISCORD_TOKEN=paste_your_discord_bot_token_here
    ```
*   Follow the **YouTube Cookie Guide** below and place the `cookies.txt` file in this directory.

### 3. Start the Bot
Start the bot in the background:
```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl restart dcbot
```

---

## 🪟 Windows Setup Guide (For Home PC Hosting)

If you want to run the bot on your own Windows computer:

### 1. Install Prerequisites
Make sure you have these installed on your Windows machine:
*   [Python 3.10+](https://www.python.org/downloads/) (Make sure to check "Add Python to PATH" during installation).
*   [Node.js](https://nodejs.org/) (Download the LTS installer).
*   [FFmpeg](https://ffmpeg.org/download.html) (Download the Windows build and add its `bin` folder to your Windows environment PATH).

### 2. Run the Installer
1.  Open the bot folder on your PC.
2.  Double-click **`setup_windows.bat`**. This will set up Python dependencies, automatically download and configure **Deno** (needed for YouTube challenge solving), and configure the PO Token server.

### 3. Configure Settings
*   Create a file named `.env` in the bot directory:
    ```env
    DISCORD_TOKEN=paste_your_discord_bot_token_here
    ```
*   Follow the **YouTube Cookie Guide** below and place the `cookies.txt` file in this directory.

### 4. Run the Bot
*   Double-click **`run_windows.bat`**. This starts both the helper token server and the Discord bot concurrently. Keep both windows open.

---

## 🍪 YouTube Cookie Guide (Bypass Bot Verification)
YouTube blocks servers or programs that download video audio unless they pass verification or log in. To bypass this, you need to provide your login cookies.

1.  Open an **Incognito/Private** window in your browser.
2.  Go to YouTube and log into a **dedicated/throwaway Google Account** (do not use your main personal account).
3.  Play any video for 5–10 seconds.
4.  Use a browser extension (such as **"Get cookies.txt LOCALLY"** for Chrome/Firefox) to export the cookies in **Netscape format**.
5.  Save the file as **`cookies.txt`** and put it in the bot root directory (`cookies.txt`).
6.  **Important**: Close the incognito browser window immediately. **Do NOT click "Sign Out"** on YouTube, or your cookies will instantly become invalid.

---

## 🎵 JioSaavn Configuration
JioSaavn integration works out of the box using public API queries and HTML scrapers to fetch high-quality audio streams and lyrics directly.
*   **No credentials needed**: JioSaavn does not require API keys or login accounts.
*   **Switching Providers**: You can switch your default search provider using the `/provider` command.

---

## 🎮 Slash Commands
All interactions with the Discord Music Player are handled via Discord's Slash Commands:

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `/play` | `song` (text / search query) | Play a song immediately or enqueue it (YouTube / JioSaavn search). |
| `/url` | `url` (link) | Play a YouTube or direct audio stream URL. |
| `/search` | `query` (text) | Search for songs on the active provider. |
| `/provider` | None | Open the interactive button menu to switch the default music provider. |
| `/queue` | None | Show the current music queue with pagination. |
| `/lyrics` | `song` (optional text) | Look up lyrics for the current song or a searched song. |
| `/volume` | `level` (integer 0-100) | Adjust the playback volume for this server. |
| `/skip` | None | Skip the currently playing track. |
| `/pause` | None | Pause the music playback. |
| `/resume` | None | Resume the paused music playback. |
| `/stop` | None | Stop the playback and clear the queue. |
| `/join` | None | Force the bot to join your active voice channel. |
| `/go` | None | Tell the bot to leave the voice channel. |
| `/bassboost`| None | Toggle the bass boost audio filter. |
| `/nightcore`| None | Toggle the nightcore audio filter (pitch up & speed up). |
| `/diagnose` | None | Diagnose voice connection and latency issues. |
| `/voicecheck`| None | Verify current voice connection details. |
| `/help` | None | Show the categorized help menu. |

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
