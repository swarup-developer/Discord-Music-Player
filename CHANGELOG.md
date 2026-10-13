# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2026-10-13] - Live TV, radio and your own files

### Added

- `/local upload` and a right-click message menu (Apps -> Play audio files):
  play up to 10 audio files from your device. Files are streamed from
  Discord and never stored on the server.
- `/remove` drops a song from the queue by its number.
- `/radio search` and a live `/radio list` backed by Radio Browser, with the
  bundled presets as an offline fallback.
- Background health check for TV streams (India first, every 12 hours). Dead
  channels are hidden from browse, search and play.

### Fixed

- `/tv browse` crashed after choosing a country, and `/tv play` crashed with a
  country (`COUNTRY_NAME_TO_CODE` was never defined).
- Every Indian TV channel was labelled Hindi; real languages are used now.
- Streams that need a specific User-Agent or Referer (711 channels) now play.
- News channels such as India TV and News18 played no audio (FFmpeg probe too
  small for HLS); live HLS streams now get a larger probe.
- The bot said "Now streaming" even when a stream failed, and showed URL parts
  such as `index.m3u8` instead of the channel or station name.
- Radio servers that refuse HEAD requests were reported unreachable.
- Radio menus broke on stream URLs longer than 100 characters.
- Dead radio presets replaced with tested stations.
- httpx no longer logs every request.


## [2026-10-13] - YouTube Music & automatic cookies

YouTube Music, a much faster YouTube path, and no more manual cookies.

### Added

- **YouTube Music provider.** `/provider` now offers YouTube Music, which
  searches official songs instead of videos (no music-video intros). Spotify
  tracks are matched on YouTube Music too.
- **Automatic cookie refresher** (`ytjs/cookie_refresher.mjs`, systemd service
  `dcbot-cookies`). A headless Chromium on the server opens YouTube every
  `COOKIE_REFRESH_HOURS` (default 6) and atomically replaces `cookies.txt`.
  Runs at the lowest CPU/IO priority, capped at 450 MB RAM and 50% CPU.
- **`YOUTUBE_PROXY` setting.** Routes all YouTube traffic (resolver, yt-dlp,
  FFmpeg) through a proxy. `setup.sh` installs Cloudflare WARP in proxy mode
  (`127.0.0.1:40000`) and sets it, which gets past "Sign in to confirm you're
  not a bot" on datacenter IPs without any cookies.
- Dead cookie files are retired automatically: a signed-out or expired
  `cookies.txt` is renamed to `cookies.txt.invalid` instead of being used.
- The resolver reloads `cookies.txt` on change, without a restart.
- `requirements.txt` and `.env.example`.

### Changed

- **YouTube is 3-10x faster.** The local youtubei.js resolver is now the
  primary path (search ~0.7 s, stream ~0.5-2 s) instead of a fallback; yt-dlp
  (~5 s per track) is only used when the resolver fails. The resolver keeps
  one warm session, deduplicates concurrent requests and caches stream URLs.
- **JioSaavn search** uses `search.getResults` again (it works again), with
  autocomplete as the fallback. Ranking now prefers the exact original song,
  merges compilation re-uploads of the same recording, pushes covers and
  lofi/slowed/remix versions down unless asked for, and puts newer songs first
  among equal matches. The duplicate base request was removed.
- The plain YouTube provider was replaced by YouTube Music; saved "youtube"
  settings switch over automatically. YouTube links still play.
- `setup.sh` now installs Node.js 22, WARP, Playwright Chromium and the cookie
  refresher, and installs Python packages from `requirements.txt`.
- README: new "Automatic YouTube Cookies" and "How YouTube Playback Works"
  sections; outdated Invidious/Cobalt and manual-cookie instructions replaced.

### Fixed

- Tracks that went silent after about a second: stream URLs were only checked
  for their first 1 KB, while YouTube refused everything past ~1 MB. URLs are
  now checked deep inside the file, with a fallback to a fully playable format.
- The youtubei.js resolver never worked: dependencies were not installed, it
  had no JavaScript evaluator for deciphering, and it tried a client that
  returns no usable URLs first.
- YouTube search crashed when earlier search methods returned nothing (it
  called a removed Invidious helper).
- Playing a local file picked from search results crashed (tuple unpacking).
- yt-dlp overwrote the shared `cookies.txt` with its own jar; it now works on a
  private copy.
- `setup.sh` installed Node 20, which silently ignores the proxy settings the
  resolver needs (requires Node >= 22.21).
- `deploy.py` printed the sudo password in its output.

### Removed

- Dead Invidious search code and README instructions for the defunct
  Invidious/Cobalt fallbacks.

## [2026-09-04]

### Added

- A local YouTube stream resolver (`ytjs/`, Node + youtubei.js) that runs beside
  the POT server on `127.0.0.1:4417` and is now the fallback used when yt-dlp
  cannot extract a stream. Startup reports whether it is listening.
- Startup check that reports, by name, which YouTube authentication cookies are
  missing from `cookies.txt`. An export taken from a signed-out browser profile
  looks valid but makes YouTube reject every extraction with "Sign in to
  confirm you're not a bot", which previously surfaced only as a generic
  playback failure. Only cookie names are read; values are never logged.
- The PO Token provider check now probes port 4416 as well as importing the
  yt-dlp plugin. The two fail independently, so a loaded plugin alone never
  proved the provider was actually reachable.
- Playback now logs the EQ preset, effects and volume in force for each track,
  so unexpected audio processing can be traced to persisted server settings.
- `fix_pot.bat` — one-step repair and verification for the PO Token provider on
  Windows. Syncs dependencies (`uv`, with a pip fallback), then verifies that
  the yt-dlp plugin is importable and reports whether the POT server is
  listening on port 4416.
- Startup diagnostic that logs an explicit error when the PO Token provider
  plugin is missing, instead of letting YouTube playback degrade silently.

### Changed

- Deployment is now incremental: only eligible changed source files are uploaded.
  Documentation, logs, credentials, caches, private audio, and generated files
  remain local.
- Deployment now checks installed dependencies before running package managers,
  so healthy VPS installations are not reinstalled on every deploy.
- YouTube extraction now tries yt-dlp first and uses the local ytjs resolver only
  when the primary extractor fails, keeping normal playback logs clean.

- **YouTube extraction no longer falls back to public Invidious/Cobalt
  instances.** Every instance in the list was offline, rate-limited, redirecting
  or returning a stream with zero bytes, so the fallback was guaranteed to fail
  in exactly the situation it existed for. Extraction now falls back to a local
  resolver that talks to YouTube's own API from the same machine, and the
  resolved URL is checked for actual audio bytes before playback uses it.
- The equalizer presets now live in one place instead of being duplicated
  across every provider, so a preset cannot drift between YouTube, JioSaavn,
  SoundCloud and local playback.

### Fixed

- **Rewind/Forward and EQ changes are now near-instant.** Seeking rebuilt the
  track from scratch — provider search plus a fresh stream extraction — before
  restarting playback, so each ±10s press waited seconds on the network. The
  resolved stream is now reused and only FFmpeg restarts; a full re-resolve
  happens solely when the cached stream is no longer usable.
- **Silent stop after seeking or changing an audio setting.** Both actions stop
  the current track before rebuilding it, with queue advancement disabled, so a
  failed rebuild left the bot quiet with no message anywhere and nothing to
  play next. The failure is now reported in chat and logged with the provider
  and track.

- **YouTube playback reliability** (intermittent silent failures):
  - `bgutil-ytdlp-pot-provider` is now declared as a project dependency. The POT
    server was being started while the yt-dlp client plugin was absent from the
    dependency list, so dependency syncs removed it and yt-dlp ran without a PO
    token — making extraction slow and prone to failing outright.
  - Raised the yt-dlp extraction cap from 2.5s, which sat below yt-dlp's normal
    latency and so caused it to lose every extraction race and forced playback
    onto public fallback instances. Start latency is unchanged: it is decided by
    the extraction race, and the cap now only prevents a wedged extraction from
    hanging playback.
  - Blocking yt-dlp extractions now run on a dedicated bounded thread pool. A
    timeout cannot cancel a thread that is already running, so abandoned
    extractions accumulated in the shared pool and progressively starved other
    blocking work the longer the bot ran.
- **Playback Controller Next/Previous buttons** now work reliably:
  - `after`-playback callbacks are generation-guarded, so a stopped track can no
    longer overwrite the state of a newer track during Next/Prev/seek/reload.
  - Queued tracks keep their resolved song data, so advancing the queue replays
    the exact song instead of re-searching a raw ID that fails to resolve.
  - Next and `/skip` now advance to the next queue item even in repeat-one mode
    instead of replaying the same song.
- **Spotify integration**:
  - Spotify albums/playlists play their first track correctly on all providers
    (previously failed on JioSaavn by resolving the song by raw ID).
  - Spotify links are accepted directly by `/play`.
- **Audio streaming lag**: removed the artificial per-frame sleep in the audio
  wrapper that throttled playback below real time and caused lag/stutter.
- **YouTube metadata**: fall back to the search-result title when a fallback
  extraction returns no metadata (no more "Unknown YouTube Video" titles).
- **JioSaavn**:
  - Song links of the form `/song/<slug>/<token>` now extract the correct token
    and play (previously the slug was used and playback failed).
  - Skip redundant song-detail re-fetches when search results already contain
    media info, making queue advancement and Next faster.

## [2026-07-23]

### Added

- Rewind (`-10s`) and Forward (`+10s`) buttons on the Playback Controller.
- Clear Queue button on the Playback Controller.
- Playback Controller accessibility improvements for PC screen readers
  (NVDA, JAWS, Narrator) and mobile screen readers.
- `/play` commands now stop the current track and play the requested song
  immediately instead of queueing it.

### Fixed

- Removed emoji from controller buttons for NVDA compatibility.
- Restored the Playback Controller embed.
- YouTube direct stream playback by sending the correct User-Agent header.
- YouTube playback hangs by adding a timeout to the primary yt-dlp extraction.

### Performance

- Sub-second YouTube playback via direct FFmpeg streaming and a concurrent
  stream-extraction race (yt-dlp vs. Invidious/Cobalt fallbacks).

## [2026-07-22]

### Added

- IPTV: curated, verified Indian TV streams plus an indexed playlist of
  690+ channels, with language/country browsing.
- Default music provider set to JioSaavn; `/play` and `/provider` restored.

### Fixed

- Bound the IPTV channel-loading wait loop so commands no longer freeze.
- Fixed an indentation error in `bot/tv.py` that crashed startup.
- YouTube streaming now prioritizes yt-dlp with cookies before falling back to
  public APIs.

## [2026-07-21] and earlier

- Restored original voice-join behavior (20s timeout, no reconnect).
- Excluded local deployment/credential files from git and deploys.
- Removed a duplicate `/stop` command that caused a `CommandAlreadyRegistered`
  crash at startup.
- YouTube Shorts and video playback fixes via dynamic cookie path resolution and
  updated Cobalt fallbacks.
- JioSaavn search/stream stability and crash fixes.
- Direct stream (IPTV/M3U8) reachability verification with clear timeout
  reporting.
- Fast YouTube scrape-based search and yt-dlp signature challenge handling.
