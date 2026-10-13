# Discord Music Player

Hi! This is a simple, fast, and self-hosted Discord music bot. I built this to play music instantly in your voice channels with interactive buttons, audio filters (like bassboost and nightcore), and a built-in solution to bypass YouTube's bot verification blocks. It works great on both Linux (VPS) and Windows.

---

## Disclaimer
This is a personal, open-source project. I have no official association with Google (YouTube), JioSaavn, or any other music service. This bot is intended for personal and educational use only, so please use it responsibly.

---

## 🚀 Key Features
*   **Instant Playback**: Plays music in your voice channel immediately without buffering lags.
*   **Full Screen Reader & Mobile Accessibility**: Designed with clean, sequential top-to-bottom text readouts and emoji-free button labels. Fully accessible for PC screen readers (NVDA, JAWS, Windows Narrator) and mobile devices (Apple VoiceOver, Android TalkBack).
*   **Fast YouTube Music**: A local resolver (youtubei.js) keeps one warm YouTube session, so search plus stream start takes about 1-3 seconds instead of 30+. Searches YouTube Music songs (official audio, no video intros).
*   **No Cookie Babysitting**: On a VPS, YouTube traffic goes through Cloudflare WARP, and a server-side cookie refresher keeps `cookies.txt` fresh automatically. Dead cookie files are retired on their own. You never have to export cookies by hand.
*   **Challenge Solver**: Automatically solves YouTube's signature challenges in the background.
*   **Interactive Control Board**: Clean, accessible Playback Controller with play, pause, rewind (-10s), forward (+10s), skip, clear queue, filter, and volume controls.
*   **Fast Dependencies**: Managed with uv for lightning-fast installation and updates.
*   **Startup Sound**: Plays a customizable audio file when the bot joins a voice channel.
*   **Live TV & Radio**: Browse and stream live television feeds (by language or country selection) and pre-configured internet radio stations.
*   **Custom Default Providers**: Set YouTube Music, JioSaavn, SoundCloud, Spotify, or Local Files as the default music search/playback provider.
*   **Smart JioSaavn Results**: The original song comes first; compilation re-uploads are merged, and covers or lofi/slowed versions only show up when you ask for them.
*   **Spotify Links**: Track, album and playlist links work without a Spotify account. Songs are matched and played through YouTube Music.

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
This script installs everything: Python, Node.js 22, Deno, FFmpeg, Supervisor, the PO token server, the YouTube resolver, Cloudflare WARP (proxy mode) and the automatic cookie refresher:
```bash
git clone https://github.com/swarup-developer/Discord-Music-Player.git
cd Discord-Music-Player
sudo bash setup.sh
```

### 2. Add Your Token:
The installer creates `.env` from `.env.example`. Open it and set your token:
```env
DISCORD_TOKEN=your_token_here
```
That's it. **No cookies needed**: see [Automatic YouTube Cookies](#-automatic-youtube-cookies-server) below.

### 3. Run:
Supervisor manages the bot in the background:
*   "sudo supervisorctl status" - Check that `dcbot`, `dcbot-ytjs` and `bgutil-pot` are running.
*   "sudo supervisorctl restart dcbot" - Restart the bot.
*   "systemctl status dcbot-cookies" - Check the automatic cookie refresher.

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

### 3. Add Your Token:
*   Copy `.env.example` to `.env` and set your token:
    ```env
    DISCORD_TOKEN=your_token_here
    ```
*   On a home connection YouTube usually works without cookies or a proxy. If you get "Sign in to confirm you're not a bot", see the cookie section below.

### 4. Run:
*   Double-click "run_windows.bat" to start the bot, the token helper and the YouTube resolver. Keep the windows open.

---

## 🍪 Automatic YouTube Cookies (Server)

YouTube blocks most VPS / datacenter IP addresses with "Sign in to confirm you're not a bot". Hand-exported browser cookies only fix that for a while: YouTube rotates the session, the file dies, and the bot asks for cookies again. This project solves it on the server, with nothing to run on your own PC.

### How it works
1.  **Cloudflare WARP (proxy mode)** runs on the server at `127.0.0.1:40000`. Only YouTube traffic is sent through it, via `YOUTUBE_PROXY` in `.env`. The resolver, yt-dlp and FFmpeg all use the same proxy, because YouTube stream links only work from the IP that requested them. SSH and everything else on the server are untouched.
2.  **The cookie refresher** (`ytjs/cookie_refresher.mjs`, systemd service `dcbot-cookies`) starts a headless Chromium every few hours, opens YouTube through WARP, and replaces `cookies.txt` atomically (the old file is swapped out in one step, never half-written).
3.  **Dead cookie files are retired automatically.** At startup the bot checks `cookies.txt`. A signed-out or expired export is renamed to `cookies.txt.invalid` instead of being used.
4.  **yt-dlp works on a private copy** of the cookie file, so it can never overwrite the refresher's output.

`setup.sh` installs and starts all of this. To check it:
```bash
warp-cli --accept-tos status          # should say: Connected
systemctl status dcbot-cookies        # should be: active (running)
tail -n 5 ytjs/cookies.out.log        # "Refreshed N cookies ..."
```

### Settings (`.env`)
| Variable | Default | Meaning |
|---|---|---|
| `YOUTUBE_PROXY` | `http://127.0.0.1:40000` | Proxy for all YouTube traffic. Remove it to connect directly. |
| `COOKIE_REFRESH_HOURS` | `6` | Hours between cookie refreshes. |
| `YOUTUBE_MUSIC_SEARCH` | `1` | `0` searches regular YouTube videos instead of YouTube Music songs. |

### Resource use
The refresher runs at the lowest CPU and disk priority, capped at 450 MB RAM and 50% CPU, so it cannot slow the bot down even on a 1 GB server. It skips images, video and fonts while loading YouTube.

### What about signed-in cookies?
The refresher collects **anonymous visitor cookies**, which is all playback needs behind WARP. It cannot sign in to Google for you, and Google blocks automated logins. If you want signed-in cookies (for age-restricted videos, for example), sign a **dedicated throwaway Google account** into the refresher's browser profile (`ytjs/.browser-profile`) once; from then on the refresher keeps those login cookies rotated and fresh automatically. Never use your main Google account.

### Without WARP (manual cookies)
If you can't use WARP, you can still export cookies by hand: open a private window, sign in to a throwaway Google account on YouTube, play a video, export cookies in Netscape format (for example with the "Get cookies.txt LOCALLY" extension), save them as `cookies.txt` in the bot folder, and close the private window without signing out. The resolver reloads a new `cookies.txt` automatically, without a restart.

---

## 🎵 JioSaavn Support
JioSaavn works out of the box and doesn't require any login or API keys. You can change your default music search provider using the "/provider" command.

---

## 🎮 Slash Commands
Type "/" in your Discord server to see the commands:

*   /play [song] - Plays a song by name or queues it.
*   /url [link] - Plays a YouTube or direct audio stream link.
*   /search [query] - Search for songs on your current provider.
*   /provider - Set the default music provider (YouTube Music / JioSaavn / SoundCloud / Spotify / Local Files).
*   /queue - Shows what songs are playing next.
*   /remove [number] - Removes a song from the queue (number as shown in /queue).
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

### 📺 IPTV Live TV Commands
*   /tv browse - Browse live TV feeds using a step-by-step selection flow (Language -> Country -> Category).
*   /tv play [channel] - Play a specific live TV channel by name (best working match).
*   /tv search [query] - Search TV channels and select one from the list.

Channels come from the public [iptv-org](https://github.com/iptv-org/iptv) directory. The bot checks every stream in the background (India first, every 12 hours) and hides dead channels. Paid channels that have no free public stream (for example Star Plus) are not available.

### 📻 Internet Radio Commands
*   /radio list [country] - Most-played working stations (India by default, or a two-letter country code).
*   /radio search [query] - Find stations by name or genre (e.g. Mirchi, Bollywood, lofi, jazz).
*   /radio play [url] - Play a custom radio stream URL.

Stations come from [Radio Browser](https://www.radio-browser.info), a free directory that checks streams daily. If it is unreachable, the bot falls back to `bot/radio_stations.json`.

### 📁 Your Own Files
*   /local upload - Play up to 10 audio files from your device. They are streamed from Discord and never stored on the server.
*   Right-click a message with audio files -> Apps -> **Play audio files** - plays every file attached to it. Drop up to 10 files into the chat at once (select all in a folder).
*   /local list - List audio files in the server's `audio/` directory.
*   /local play [filename] - Play a file from the server's `audio/` directory.

---

## 💾 Persistent Server Settings

The bot now remembers your server preferences. Any settings you configure on a server (guild) are automatically saved locally to `server_settings.json` and will persist even if the bot is restarted or leaves/rejoins a voice channel:
*   **Volume**: Remembers your preferred music volume level.
*   **Provider**: Remembers your chosen search provider (YouTube Music, JioSaavn, SoundCloud, Spotify or Local Files).
*   **Loop Mode**: Remembers if repeat/loop is active.
*   **Audio Effects**: Remembers active filters (like bassboost or nightcore).

---

## ⚡ How YouTube Playback Works

1.  **Search**: The local resolver (`ytjs/server.mjs`, port 4417, loopback only) searches YouTube Music through one warm InnerTube session (about 0.7 s).
2.  **Stream**: The same resolver gets a playable audio URL (about 0.5-2 s). Every URL is checked deep inside the file before use, so a stream that would cut out after a second is never handed to FFmpeg.
3.  **Fallback**: If the resolver fails, the bot falls back to yt-dlp with the PO token provider (slower, about 5 s).

Spotify links are read from Spotify's public embed (no account needed); each track is then matched on YouTube Music. Spotify's own audio is copy-protected, so no bot can stream it directly, even with Premium.

---

## 🩺 Troubleshooting

### ❌ YouTube says "Sign in to confirm you're not a bot" or LOGIN_REQUIRED
*   Check WARP: `warp-cli --accept-tos status` should say **Connected**. If not, run `warp-cli --accept-tos connect`.
*   Check that `.env` contains `YOUTUBE_PROXY=http://127.0.0.1:40000`, then run `sudo supervisorctl restart dcbot-ytjs dcbot`.
*   Check Node.js is 22.21 or newer (`node -v`); older versions ignore the proxy.
*   If WARP's IP gets blocked too, put a residential proxy URL in `YOUTUBE_PROXY` instead.

### ❌ YouTube is slow to start
*   Check the resolver: `tail -n 5 ytjs/out.log` should show `resolved ... in ~1000ms`. If it's missing, run `sudo supervisorctl restart dcbot-ytjs`.

### ❌ Playback control buttons are not showing up
*   This happens if the track failed to load or play (usually a YouTube block or a voice connection issue). Fix the cause above and the buttons return on the next play.

### ❌ Check logs on Linux:
```bash
tail -n 50 err.log              # bot errors
tail -n 20 music_bot.log        # bot activity
tail -n 20 ytjs/out.log         # YouTube resolver
tail -n 5 ytjs/cookies.out.log  # cookie refresher
```
