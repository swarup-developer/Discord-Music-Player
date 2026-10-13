# Discord Music Player

A fast, self-hosted Discord music bot. It plays YouTube Music, JioSaavn, SoundCloud and Spotify links, live TV audio, internet radio, and audio files from your own device, with an accessible button controller and audio filters. It runs on a Linux server or a Windows PC.

> **Disclaimer:** This is a personal, open-source project with no affiliation with Google (YouTube), Spotify, JioSaavn or any other service. It is intended for personal and educational use. Please use it responsibly.

## Contents

- [Features](#features)
- [Create the Discord bot](#create-the-discord-bot)
- [Hosting on Linux](#hosting-on-linux)
- [Hosting on Windows](#hosting-on-windows)
- [YouTube on a server: WARP and automatic cookies](#youtube-on-a-server-warp-and-automatic-cookies)
- [Commands](#commands)
- [Live TV and radio sources](#live-tv-and-radio-sources)
- [Configuration](#configuration)
- [How YouTube playback works](#how-youtube-playback-works)
- [Troubleshooting](#troubleshooting)

## Features

- **Fast playback.** A local YouTube resolver keeps one session open, so a song usually starts within 1 to 3 seconds.
- **YouTube Music search.** Searches return official songs rather than music videos.
- **Spotify links without an account.** Track, album and playlist links are matched and played through YouTube Music.
- **Accurate JioSaavn results.** The original song comes first. Re-uploads are merged, and covers or lofi versions only appear when you ask for them.
- **No manual cookies on a server.** YouTube traffic goes through Cloudflare WARP, and a background service keeps cookies fresh automatically.
- **Live TV and radio.** Thousands of public TV channels and radio stations. Offline TV channels are detected and hidden.
- **Your own files.** Play audio files from your phone or computer. They are streamed from Discord and never stored on the server.
- **Accessible controller.** Previous, -10s, pause, +10s, next, loop, shuffle, clear queue, volume and an equalizer menu, with plain-text labels that work with screen readers (NVDA, JAWS, Narrator, VoiceOver, TalkBack).
- **Saved settings.** Volume, provider, loop mode, audio effects and equalizer preset are remembered per server.
- **Startup sound.** Optionally plays `audio/audio.mp3` when the bot joins a voice channel. Files in `audio/` are ignored by git.

## Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Give it a name and click **Create**.
3. Open the **Bot** page and turn on **Message Content Intent** under Privileged Gateway Intents. Click **Save Changes**.
4. On the same page, click **Reset Token** and copy the token. Treat it like a password and never share it.
5. Open **OAuth2 > URL Generator**. Under Scopes select `bot` and `applications.commands`. Under Bot Permissions select Send Messages, Embed Links, Connect, Speak and Use Voice Activity.
6. Open the generated URL in your browser and add the bot to your server.

## Hosting on Linux

Written for Ubuntu 22.04. The installer sets up Python, Node.js 22, Deno, FFmpeg, Supervisor, the PO token server, the YouTube resolver, Cloudflare WARP and the cookie refresher.

```bash
git clone https://github.com/swarup-developer/Discord-Music-Player.git
cd Discord-Music-Player
sudo bash setup.sh
```

The installer creates `.env` from `.env.example`. Add your token:

```env
DISCORD_TOKEN=your_token_here
```

Then restart the bot:

```bash
sudo supervisorctl restart dcbot
```

Useful service commands:

| Command | Purpose |
|---|---|
| `sudo supervisorctl status` | Check that `dcbot`, `dcbot-ytjs` and `bgutil-pot` are running |
| `sudo supervisorctl restart dcbot` | Restart the bot |
| `systemctl status dcbot-cookies` | Check the cookie refresher |
| `warp-cli --accept-tos status` | Check Cloudflare WARP |

To update an existing installation, pull the latest code and run `sudo bash setup.sh` again.

## Hosting on Windows

1. Install [Python 3.10 or newer](https://www.python.org/downloads/), [Node.js 22 LTS](https://nodejs.org/) and [FFmpeg](https://ffmpeg.org/download.html).
2. In the project folder, run `setup_windows.bat`. It creates the virtual environment and installs the dependencies.
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
4. Run `run_windows.bat`. It starts the bot, the PO token server and the YouTube resolver. Keep the windows open.

On a home internet connection YouTube usually works without a proxy or cookies.

## YouTube on a server: WARP and automatic cookies

YouTube blocks most server and datacenter IP addresses with the message "Sign in to confirm you're not a bot". Exported browser cookies only fix this for a short time, because YouTube rotates the session and the file stops working. This project handles it on the server with no manual steps.

1. **Cloudflare WARP** runs in proxy mode at `127.0.0.1:40000`. Only YouTube traffic uses it, through `YOUTUBE_PROXY` in `.env`. SSH and all other traffic on the server are not affected. The resolver, yt-dlp and FFmpeg use the same proxy, because YouTube stream links only work from the address that requested them.
2. **The cookie refresher** (`ytjs/cookie_refresher.mjs`, systemd service `dcbot-cookies`) opens YouTube in a headless browser every few hours and replaces `cookies.txt` in a single atomic step.
3. **Invalid cookie files are retired automatically.** At startup the bot checks `cookies.txt`, and a signed-out or expired file is renamed to `cookies.txt.invalid` instead of being used.
4. **yt-dlp uses a private copy** of the cookie file, so it never overwrites the refresher's output.

The refresher runs at the lowest CPU and disk priority, limited to 450 MB of memory and 50% CPU, so it does not slow the bot down even on a 1 GB server.

**Signed-in cookies.** The refresher collects anonymous visitor cookies, which is enough for playback through WARP. It cannot sign in to Google for you. If you need signed-in cookies, for example for age-restricted videos, sign a dedicated spare Google account into the refresher's browser profile (`ytjs/.browser-profile`) once. The refresher then keeps those cookies fresh. Never use your main Google account.

**Without WARP.** You can export cookies manually instead. Open a private browser window, sign in to a spare Google account on YouTube, play a video, export the cookies in Netscape format, save them as `cookies.txt` in the project folder, and close the window without signing out. The resolver picks up a new `cookies.txt` without a restart.

## Commands

Type `/` in Discord to see all commands. Use `/join` first so the bot is in your voice channel.

### Music

| Command | Description |
|---|---|
| `/play <song>` | Play a song by name or link, or add it to the queue |
| `/search <query>` | Search on the current provider and pick a result |
| `/url <link>` | Play a YouTube link or a direct audio stream |
| `/provider` | Choose the default source: YouTube Music, JioSaavn, SoundCloud, Spotify or Local Files |
| `/lyrics [song]` | Show lyrics for the current or a given song |

### Playback and queue

| Command | Description |
|---|---|
| `/queue` | Show the queue |
| `/remove <number>` | Remove a song from the queue by its number in `/queue` |
| `/skip` | Skip to the next song |
| `/pause`, `/resume` | Pause or resume playback |
| `/stop` | Stop playback and clear the queue |
| `/volume <1-200>` | Set the volume |
| `/bassboost`, `/nightcore` | Toggle audio filters |
| `/join`, `/go` | Join or leave your voice channel |
| `/diagnose`, `/voicecheck` | Check the voice connection |
| `/help` | Show the help menu |

### Your own files

| Command | Description |
|---|---|
| `/local upload` | Play up to 10 audio files from your device |
| Right-click a message, then Apps > **Play audio files** | Play every audio file attached to that message |
| `/local list` | List audio files in the server's `audio/` folder |
| `/local play <file>` | Play a file from the server's `audio/` folder |

Uploaded files are streamed from Discord and are never stored on the server. To play many files, drop up to 10 at a time into the chat and use the message menu.

### Live TV and radio

| Command | Description |
|---|---|
| `/tv browse` | Browse channels by language, country and category |
| `/tv search <name>` | Search channels and pick one |
| `/tv play <name>` | Play the best working match |
| `/radio list [country]` | Most popular working stations, India by default (use a two-letter country code) |
| `/radio search <name or genre>` | Find stations by name or genre |
| `/radio play <url>` | Play any radio stream URL |

## Live TV and radio sources

- **TV** channels come from the public [iptv-org](https://github.com/iptv-org/iptv) directory. The bot checks every stream in the background every 12 hours and hides channels that are offline. Paid channels without a free public stream are not available.
- **Radio** stations come from [Radio Browser](https://www.radio-browser.info), a free directory that checks its streams daily. If it cannot be reached, the bot uses the stations in `bot/radio_stations.json`.

## Configuration

All settings live in `.env`. See `.env.example` for the full list.

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | (required) | Your bot token |
| `BOT_SYNC_GUILD_ID` | (empty) | Register commands instantly in one server |
| `YOUTUBE_PROXY` | `http://127.0.0.1:40000` | Proxy for all YouTube traffic. Remove it to connect directly |
| `YOUTUBE_MUSIC_SEARCH` | `1` | Set to `0` to search regular YouTube videos instead of YouTube Music |
| `COOKIE_REFRESH_HOURS` | `6` | Hours between automatic cookie refreshes |

Python dependencies are listed in `requirements.txt` and `pyproject.toml`. Node.js dependencies for the resolver are in `ytjs/package.json`.

## How YouTube playback works

1. **Search.** The local resolver (`ytjs/server.mjs`, port 4417, local only) searches YouTube Music through a session that stays open.
2. **Stream.** The resolver returns a playable audio link. Each link is checked before use, so a stream that would stop after a second is never played.
3. **Fallback.** If the resolver fails, the bot uses yt-dlp with the PO token provider. This is slower, about 5 seconds per song.

Spotify links are read from Spotify's public page, and each track is matched on YouTube Music. Spotify's own audio is copy-protected and cannot be streamed by any bot, with or without Premium.

## Troubleshooting

**YouTube says "Sign in to confirm you're not a bot"**

- Check WARP with `warp-cli --accept-tos status`. It should say Connected. If not, run `warp-cli --accept-tos connect`.
- Check that `.env` contains `YOUTUBE_PROXY=http://127.0.0.1:40000`, then run `sudo supervisorctl restart dcbot-ytjs dcbot`.
- Check that Node.js is 22.21 or newer with `node -v`. Older versions ignore the proxy setting.
- If the WARP address is also blocked, set `YOUTUBE_PROXY` to a residential proxy.

**YouTube is slow to start**

- Run `tail -n 5 ytjs/out.log`. It should show lines like `resolved ... in 1000ms`. If not, run `sudo supervisorctl restart dcbot-ytjs`.

**The controller buttons do not appear**

- The track failed to load or play, usually because of a YouTube block or a voice connection problem. Fix the cause above and the buttons return on the next song.

**Logs on Linux**

```bash
tail -n 50 err.log              # bot errors
tail -n 20 music_bot.log        # bot activity
tail -n 20 ytjs/out.log         # YouTube resolver
tail -n 5 ytjs/cookies.out.log  # cookie refresher
```

## License

See [LICENSE](LICENSE).
