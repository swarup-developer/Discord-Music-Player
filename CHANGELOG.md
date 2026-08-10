# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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
- **YouTube playback**:
  - Extended the yt-dlp extraction window so direct stream extraction can finish
    when public fallback instances are unavailable.
  - Fall back to the search-result title when a fallback extraction returns no
    metadata (no more "Unknown YouTube Video" titles).
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
