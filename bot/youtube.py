import asyncio
import json
import logging
import os
import re
import socket
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Any

import discord
import httpx
import yt_dlp

from .audio_source import apply_seek, build_ffmpeg_options, make_stream_source

# FFmpeg input options, kept as ``-ss``-free templates so a later seek can be
# applied with apply_seek() without re-resolving the stream.
_HTTP_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-headers \"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n\" "
    "-nostdin -probesize 100000 -analyzeduration 100000 -fflags nobuffer"
)
_FILE_BEFORE_OPTIONS = "-nostdin -probesize 100000 -analyzeduration 100000 -fflags nobuffer"


def build_http_before_options(http_headers: Optional[dict] = None) -> str:
    """Build FFmpeg before_options matching yt-dlp's player client request headers."""
    if http_headers:
        filtered = {k: v for k, v in http_headers.items() if k.lower() in ("user-agent", "accept", "accept-language", "sec-fetch-mode")}
        if not any(k.lower() == "user-agent" for k in filtered):
            filtered["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        header_lines = "".join(f"{k}: {v}\r\n" for k, v in filtered.items())
        headers_opt = f'-headers "{header_lines}" '
    else:
        headers_opt = '-headers "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n" '
    return (
        f"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        f"{headers_opt}"
        f"-nostdin -probesize 100000 -analyzeduration 100000 -fflags nobuffer"
    )

logger = logging.getLogger(__name__)

# Safety-net cap for a single yt-dlp extraction. Start latency is decided by
# the yt-dlp/fallback race in extract_stream_url_async, not by this value, so
# it is deliberately generous: a cap tighter than yt-dlp's real latency just
# guarantees the fallbacks win every time. Observed successful extractions have
# taken ~8s when YouTube is throttling, so keep well clear of that.
_YDL_EXTRACT_TIMEOUT = 45.0

# Dedicated bounded pool for blocking yt-dlp calls. asyncio.wait_for cannot
# cancel a thread that is already running, so timed-out extractions keep
# occupying a worker until yt-dlp gives up. Isolating them here caps the damage
# and keeps the default executor free for everything else the bot does.
_YDL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ydl")


POT_SERVER_HOST = "127.0.0.1"
POT_SERVER_PORT = 4416

# Local youtubei.js resolver (ytjs/server.mjs), the fallback used when yt-dlp
# cannot extract a stream. Loopback only -- see the security note in server.mjs.
YTJS_SERVER_HOST = "127.0.0.1"
YTJS_SERVER_PORT = 4417
# Generous enough to cover a cold InnerTube session plus a client retry, but
# still well inside _YDL_EXTRACT_TIMEOUT so the fallback can win the race.
YTJS_TIMEOUT = 40.0

# Cookie names yt-dlp actually authenticates YouTube with. A logged-in Netscape
# export contains the first-party set; an export taken while signed out (or by
# an extension that only captures third-party cookies) contains just the "3P"
# variants, and YouTube then answers every request from a datacenter IP with
# "Sign in to confirm you're not a bot".
_AUTH_COOKIE_NAMES = (
    "__Secure-1PSID",
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
)


def check_pot_provider() -> bool:
    """Warn at startup if the PO Token provider plugin or server is missing.

    run_windows.bat and setup.sh launch the bgutil POT *server* on port 4416,
    but yt-dlp can only reach it through the bgutil-ytdlp-pot-provider client
    plugin. Without the plugin YouTube extraction still "works" -- it just
    becomes slow and intermittently fails, with no error pointing at the cause.
    The plugin is easy to lose because `uv sync` prunes anything missing from
    pyproject.toml, so surface it loudly instead of debugging playback again.

    A loaded plugin does NOT prove the server is reachable, so probe the port
    too -- the two fail independently.
    """
    ok = True
    try:
        import yt_dlp_plugins.extractor  # noqa: F401
        logger.info("PO Token provider plugin loaded.")
    except ImportError:
        logger.error(
            "PO Token provider plugin not found. YouTube playback will be slow "
            "and unreliable. Install it with: uv sync (or pip install "
            "bgutil-ytdlp-pot-provider>=1.3.1)"
        )
        ok = False

    sock = socket.socket()
    sock.settimeout(1.5)
    try:
        if sock.connect_ex((POT_SERVER_HOST, POT_SERVER_PORT)) == 0:
            logger.info("POT server is listening on %s:%s.", POT_SERVER_HOST, POT_SERVER_PORT)
        else:
            logger.error(
                "POT server is NOT listening on %s:%s. yt-dlp will get no PO "
                "token and YouTube playback will be unreliable. Start it with "
                "run_windows.bat, or `supervisorctl start bgutil-pot` on the server.",
                POT_SERVER_HOST, POT_SERVER_PORT,
            )
            ok = False
    except Exception as e:
        logger.warning("Could not probe the POT server port: %s", e)
    finally:
        sock.close()

    return ok


def check_ytjs_resolver() -> bool:
    """Warn at startup when the local youtubei.js resolver is not listening.

    It is the only remaining fallback for YouTube extraction, so if it is down
    a yt-dlp failure means no playback at all rather than a slower path.
    """
    sock = socket.socket()
    sock.settimeout(1.5)
    try:
        if sock.connect_ex((YTJS_SERVER_HOST, YTJS_SERVER_PORT)) == 0:
            logger.info(
                "Local YouTube resolver is listening on %s:%s.",
                YTJS_SERVER_HOST, YTJS_SERVER_PORT,
            )
            return True
        logger.error(
            "Local YouTube resolver is NOT listening on %s:%s. YouTube playback "
            "will have no fallback if yt-dlp fails. Start it with "
            "run_windows.bat, or `supervisorctl start dcbot-ytjs` on the server. "
            "First-time setup needs `npm install` in the ytjs directory.",
            YTJS_SERVER_HOST, YTJS_SERVER_PORT,
        )
        return False
    except Exception as e:
        logger.warning("Could not probe the local YouTube resolver port: %s", e)
        return False
    finally:
        sock.close()


def check_youtube_cookies() -> bool:
    """Warn at startup when cookies.txt is missing its authentication cookies.

    Only cookie NAMES are ever read or logged here -- never values.
    """
    cookie_file = find_cookie_file()
    if not cookie_file:
        logger.warning(
            "No cookies.txt found. YouTube will likely reject extraction with "
            "\"Sign in to confirm you're not a bot\"."
        )
        return False

    present = set()
    try:
        with open(cookie_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) >= 6:
                    present.add(fields[5])
    except OSError as e:
        logger.warning("Could not read %s: %s", cookie_file, e)
        return False

    missing = [name for name in _AUTH_COOKIE_NAMES if name not in present]
    if missing:
        logger.error(
            "%s is missing YouTube auth cookies: %s. This is an export from a "
            "signed-out session, so YouTube will reject extraction with \"Sign "
            "in to confirm you're not a bot\". Re-export from a browser profile "
            "that is signed in to YouTube, then stop using that profile.",
            cookie_file, ", ".join(missing),
        )
        return False

    logger.info("%s contains the expected YouTube auth cookies.", cookie_file)
    return True


# yt-dlp fallback config
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',  # Bind to IPv4 to prevent IPv6 timeout issues
    'remote_components': ['ejs:github'],
    'js_runtimes': {'node': {}},
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web']
        }
    },
}


def find_cookie_file() -> Optional[str]:
    """Return the first cookies.txt found in the usual locations, if any."""
    for cp in (
        "cookies.txt",
        "bot/cookies.txt",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cookies.txt"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"),
    ):
        if os.path.exists(cp):
            return cp
    return None


def get_ydl_opts(extra_opts: Optional[dict] = None) -> dict:
    opts = {**YDL_OPTIONS}

    cookie_file = find_cookie_file()
    if cookie_file:
        opts['cookiefile'] = cookie_file

    if extra_opts:
        opts.update(extra_opts)
    return opts



# Video ID extraction
_YT_ID_RE = re.compile(
    r'(?:youtube\.com/watch\?.*?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)'
    r'([a-zA-Z0-9_-]{11})'
)

_YT_PLAYLIST_RE = re.compile(r'(?:[?&]list=)([a-zA-Z0-9_-]+)')

def _extract_video_id(url: str) -> Optional[str]:
    """Pull the 11-char video ID out of a YouTube URL."""
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


def _is_youtube_playlist_url(url: str) -> bool:
    q = url.lower()
    return "youtube.com/playlist" in q or bool(_YT_PLAYLIST_RE.search(q))


# Invidious/Cobalt public instances used to live here. Every instance was
# dead, rate-limited or returning empty tunnels, so the fallback failed exactly
# when it was needed. Extraction now falls back to the local youtubei.js
# resolver in ytjs/ instead -- see YTJS_SERVER_PORT above.


class YouTubeHandler:
    @staticmethod
    def is_youtube_url(query: str) -> bool:
        q = query.lower()
        return "youtube.com" in q or "youtu.be" in q

    @staticmethod
    def is_playable_youtube_url(query: str) -> bool:
        if not YouTubeHandler.is_youtube_url(query):
            return False
        return bool(_extract_video_id(query)) or _is_youtube_playlist_url(query)

    # Piped API helpers

    # Public API functions
    @classmethod
    async def _invidious_search(cls, query: str, limit: int = 10) -> list[dict]:
        """Search YouTube videos using Invidious API in parallel."""
        import urllib.parse
        encoded_query = urllib.parse.quote(query)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        instances = cls._get_invidious_instances()

        async def _query_instance(client: httpx.AsyncClient, instance: str) -> Optional[list[dict]]:
            api_url = f"https://{instance}/api/v1/search?q={encoded_query}&type=video"
            try:
                response = await client.get(api_url, timeout=3.0)
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        results = []
                        for entry in data[:limit]:
                            if not entry or not entry.get("videoId"):
                                continue
                            results.append({
                                'id': entry.get('videoId'),
                                'title': entry.get('title') or 'Unknown YouTube Video',
                                'song': entry.get('title') or 'Unknown YouTube Video',
                                'album': entry.get('author') or 'YouTube',
                                'url': f"https://www.youtube.com/watch?v={entry.get('videoId')}",
                                'duration': entry.get('lengthSeconds'),
                                'provider': 'youtube'
                            })
                        if results:
                            cls._working_invidious_instance = instance
                            logger.info(f"Invidious search succeeded for '{query}' using {instance}")
                            return results
            except Exception as e:
                logger.debug(f"Invidious search failed on {instance}: {e}")
            return None

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            tasks = [asyncio.create_task(_query_instance(client, inst)) for inst in instances]
            pending = set(tasks)
            results = None
            
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        res = task.result()
                        if res:
                            results = res
                            break
                    except Exception:
                        pass
                if results:
                    break

            for task in pending:
                task.cancel()

            return results or []

    @classmethod
    async def _youtube_api_search(cls, query: str, limit: int = 10) -> list[dict]:
        """Search YouTube using the official Data API v3."""
        from .config import YOUTUBE_API_KEY
        if not YOUTUBE_API_KEY:
            return []
        
        import httpx
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": limit,
            "key": YOUTUBE_API_KEY
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    results = []
                    for item in data.get("items", []):
                        video_id = item.get("id", {}).get("videoId")
                        snippet = item.get("snippet", {})
                        if not video_id:
                            continue
                        title = snippet.get("title", "Unknown YouTube Video")
                        channel_title = snippet.get("channelTitle", "YouTube")
                        results.append({
                            'id': video_id,
                            'title': title,
                            'song': title,
                            'album': channel_title,
                            'url': f"https://www.youtube.com/watch?v={video_id}",
                            'duration': None,
                            'provider': 'youtube'
                        })
                    return results
                else:
                    logger.warning(f"YouTube V3 API search returned status {response.status_code}: {response.text}")
        except Exception as e:
            logger.error(f"YouTube V3 API search error: {e}")
        return []

    @classmethod
    async def _scrape_search(cls, query: str, limit: int = 10) -> list[dict]:
        """Fast YouTube search by scraping the search results page and parsing ytInitialData."""
        from http.cookiejar import MozillaCookieJar
        
        logger.info(f"Attempting fast YouTube scrape search for: '{query}'")
        
        # Load cookies
        cookies = None
        cookies_paths = [
            "cookies.txt",
            "bot/cookies.txt",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cookies.txt"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
        ]
        for cp in cookies_paths:
            if os.path.exists(cp):
                try:
                    jar = MozillaCookieJar(cp)
                    jar.load(ignore_discard=True, ignore_expires=True)
                    cookies = jar
                    logger.info(f"Loaded cookies for scrape search from: {cp}")
                    break
                except Exception as ce:
                    logger.warning(f"Failed to load cookies from {cp}: {ce}")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        
        url = "https://www.youtube.com/results"
        params = {"search_query": query}
        
        try:
            async with httpx.AsyncClient(headers=headers, cookies=cookies, follow_redirects=True, timeout=5.0) as client:
                response = await client.get(url, params=params)
                if response.status_code != 200:
                    logger.warning(f"Scrape search request failed with status: {response.status_code}")
                    return []
                
                html = response.text
                match = re.search(r"var ytInitialData\s*=\s*({.*?});", html)
                if not match:
                    match = re.search(r"window\[\"ytInitialData\"\]\s*=\s*({.*?});", html)
                
                if not match:
                    logger.warning("Failed to find ytInitialData in scrape search response")
                    return []
                
                data = json.loads(match.group(1))
                results = []
                
                try:
                    contents = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"]
                    for content in contents:
                        if "itemSectionRenderer" in content:
                            for item in content["itemSectionRenderer"]["contents"]:
                                if "videoRenderer" in item:
                                    vr = item["videoRenderer"]
                                    video_id = vr.get("videoId")
                                    if not video_id:
                                        continue
                                        
                                    title = "Unknown Video"
                                    if "title" in vr and "runs" in vr["title"] and vr["title"]["runs"]:
                                        title = vr["title"]["runs"][0].get("text", "Unknown Video")
                                    elif "title" in vr and "simpleText" in vr["title"]:
                                        title = vr["title"].get("simpleText", "Unknown Video")
                                        
                                    uploader = "YouTube"
                                    if "ownerText" in vr and "runs" in vr["ownerText"] and vr["ownerText"]["runs"]:
                                        uploader = vr["ownerText"]["runs"][0].get("text", "YouTube")
                                        
                                    duration_sec = 0
                                    if "lengthText" in vr and "simpleText" in vr["lengthText"]:
                                        simple_text = vr["lengthText"]["simpleText"]
                                        try:
                                            parts = list(map(int, simple_text.split(':')))
                                            if len(parts) == 2:
                                                duration_sec = parts[0] * 60 + parts[1]
                                            elif len(parts) == 3:
                                                duration_sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
                                        except Exception:
                                            pass
                                            
                                    results.append({
                                        'id': video_id,
                                        'title': title,
                                        'song': title,
                                        'album': uploader,
                                        'url': f"https://www.youtube.com/watch?v={video_id}",
                                        'duration': duration_sec,
                                        'provider': 'youtube'
                                    })
                                    if len(results) >= limit:
                                        break
                            if len(results) >= limit:
                                break
                except Exception as pe:
                    logger.warning(f"Error parsing ytInitialData in scrape search: {pe}")
                    
                return results
        except Exception as e:
            logger.error(f"Scrape search error: {e}")
        return []

    @classmethod
    async def search(cls, query: str, limit: int = 10) -> list[dict]:
        # For URLs, don't use search — go straight to yt-dlp
        if query.startswith("http://") or query.startswith("https://"):
            if YouTubeHandler.is_youtube_url(query) and not YouTubeHandler.is_playable_youtube_url(query):
                logger.info("Rejected non-playable YouTube URL during search: %s", query)
                return []
            return await cls._ydl_search(query, limit)

        # Try fast scrape-based search first
        scrape_results = await cls._scrape_search(query, limit)
        if scrape_results:
            return scrape_results

        # Try official YouTube API search next
        logger.info(f"Searching YouTube via official V3 API for query: '{query}'")
        api_results = await cls._youtube_api_search(query, limit)
        if api_results:
            return api_results

        # Try Invidious search next
        logger.info("YouTube V3 API search returned no results. Trying Invidious search fallback...")
        invidious_results = await cls._invidious_search(query, limit)
        if invidious_results:
            return invidious_results

        # Fallback to yt-dlp search if both fail
        logger.info("Invidious search returned no results. Trying yt-dlp search fallback...")
        return await cls._ydl_search(query, limit)

    @classmethod
    async def _ydl_search(cls, query: str, limit: int = 10) -> list[dict]:
        """yt-dlp-based search. """
        def _search():
            ydl_opts = get_ydl_opts({'playlist_items': f'1-{limit}', 'extract_flat': True})
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                if query.startswith("http://") or query.startswith("https://"):
                    try:
                        info = ydl.extract_info(query, download=False)
                        if not info:
                            return []
                        if 'entries' in info:
                            return list(info['entries'])
                        return [info]
                    except Exception as e:
                        logger.error(f"yt-dlp URL extraction error: {e}")
                        return []
                else:
                    try:
                        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                        if not info or 'entries' not in info:
                            return []
                        return list(info['entries'])
                    except Exception as e:
                        logger.error(f"yt-dlp search error: {e}")
                        return []

        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(None, _search)
        results = []
        for entry in entries:
            if not entry:
                continue
            results.append({
                'id': entry.get('id') or entry.get('webpage_url') or entry.get('url'),
                'title': entry.get('title') or 'Unknown YouTube Video',
                'song': entry.get('title') or 'Unknown YouTube Video',
                'album': entry.get('uploader') or 'YouTube',
                'url': entry.get('webpage_url') or entry.get('url'),
                'duration': entry.get('duration'),
                'provider': 'youtube'
            })
        return results

    @classmethod
    async def _validate_stream_url(cls, client: httpx.AsyncClient, url: str) -> bool:
        """Validate that the stream URL actually returns audio data (non-zero bytes).

        Skips leading empty chunks rather than treating the first one as a
        failure: tunnel endpoints that transcode on demand routinely yield an
        empty chunk while the upstream buffer fills, and rejecting those made
        working streams look dead. The read timeout still bounds the wait.
        """
        try:
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12.0) as resp:
                if resp.status_code in [200, 206]:
                    async for chunk in resp.aiter_bytes(chunk_size=1024):
                        if len(chunk) > 0:
                            return True
                    logger.warning(
                        "Stream validation got %s but no audio bytes for %s",
                        resp.status_code, url[:80]
                    )
                else:
                    logger.warning(
                        "Stream validation rejected HTTP %s for %s",
                        resp.status_code, url[:80]
                    )
        except Exception as e:
            logger.warning(f"Stream URL validation failed for {url[:80]}: {e}")
        return False

    @classmethod
    async def _extract_via_ytjs_async(cls, video_id: str) -> Optional[tuple[str, str, bool, int]]:
        """Resolve a stream through the local youtubei.js resolver.

        This replaced the Invidious and Cobalt public instances, which were all
        either offline, rate-limited or serving empty tunnels -- so the fallback
        was guaranteed to fail in exactly the situation it existed for. The
        resolver talks to YouTube's own InnerTube API from this machine, so it
        depends on nobody else's uptime.
        """
        endpoint = f"http://{YTJS_SERVER_HOST}:{YTJS_SERVER_PORT}/stream"
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(
                    endpoint, params={"id": video_id}, timeout=YTJS_TIMEOUT
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Local YouTube resolver returned HTTP %s for %s: %s",
                        resp.status_code, video_id, resp.text[:200]
                    )
                    return None
                data = resp.json()

                stream_url = data.get("url")
                if not stream_url:
                    return None
                title = data.get("title") or "Unknown YouTube Video"
                is_live = bool(data.get("isLive"))
                try:
                    duration = int(data.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0

                # A resolved googlevideo URL can still 403 from a datacenter IP,
                # which is what made the old public fallbacks useless. Confirm
                # it actually yields bytes before handing it to FFmpeg, so a
                # dead URL loses the extraction race instead of winning it and
                # producing silence. Live manifests are skipped: HLS/DASH
                # playlists are handled by FFmpeg, not read as a byte stream.
                if not is_live and not await cls._validate_stream_url(client, stream_url):
                    logger.warning(
                        "Local YouTube resolver returned an unplayable stream for %s",
                        video_id,
                    )
                    return None
        except httpx.ConnectError:
            logger.error(
                "Local YouTube resolver is not reachable on %s:%s. Start it with "
                "run_windows.bat, or `supervisorctl start dcbot-ytjs` on the server.",
                YTJS_SERVER_HOST, YTJS_SERVER_PORT,
            )
            return None
        except Exception as e:
            logger.warning(f"Local YouTube resolver failed for {video_id}: {e}")
            return None

        logger.info("Local YouTube resolver resolved %s (%r)", video_id, title)
        return stream_url, title, is_live, duration, None

    @classmethod
    def _extract_stream_url_fallback(cls, url: str) -> Optional[tuple[str, str, bool, int, Optional[dict]]]:
        """Blocking wrapper around the local resolver fallback."""
        video_id = _extract_video_id(url)
        if not video_id:
            return None
        try:
            return asyncio.run(cls._extract_via_ytjs_async(video_id))
        except RuntimeError:
            # Already inside a running loop; the async path should be used there.
            logger.warning("_extract_stream_url_fallback called from a running loop")
            return None

    @classmethod
    async def _extract_stream_url_fallback_async(cls, url: str) -> Optional[tuple[str, str, bool, int, Optional[dict]]]:
        """Resolve a stream via the local youtubei.js resolver."""
        video_id = _extract_video_id(url)
        if not video_id:
            return None
        logger.info(f"Attempting local resolver fallback for video: {video_id}")
        return await cls._extract_via_ytjs_async(video_id)

    @classmethod
    def _ydl_extract_stream_url(cls, url: str) -> Optional[tuple[str, str, bool, int, Optional[dict]]]:
        """Extract stream URL using yt-dlp as primary method, with the local resolver as fallback. Blocking."""
        logger.info(f"Attempting primary stream extraction via yt-dlp for: {url}")
        ydl_opts = get_ydl_opts({
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if info:
                    stream_url = info.get('url')
                    title = info.get('title', 'Unknown YouTube Video')
                    is_live = info.get('is_live', False) or info.get('live_status') == 'is_live'
                    duration = info.get('duration', 0)
                    http_headers = info.get('http_headers')
                    if stream_url:
                        logger.info(f"yt-dlp stream extraction succeeded for: {url}")
                        return stream_url, title, is_live, duration, http_headers
            except Exception as e:
                logger.error(f"yt-dlp extract_stream_url failed: {e}")

        # If yt-dlp fails (e.g. cookie expires or network block), try Invidious/Cobalt public fallbacks
        logger.info("yt-dlp extraction failed. Trying the local youtubei.js resolver...")
        fallback = cls._extract_stream_url_fallback(url)
        if fallback:
            return fallback

        return None

    @classmethod
    def _ydl_extract_stream_url_only(cls, url: str) -> Optional[tuple[str, str, bool, int, Optional[dict]]]:
        """Extract stream URL using yt-dlp only (blocking, no fallbacks)."""
        logger.info(f"Attempting primary stream extraction via yt-dlp only for: {url}")
        ydl_opts = get_ydl_opts({
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if info:
                    stream_url = info.get('url')
                    title = info.get('title', 'Unknown YouTube Video')
                    is_live = info.get('is_live', False) or info.get('live_status') == 'is_live'
                    duration = info.get('duration', 0)
                    http_headers = info.get('http_headers')
                    if stream_url:
                        logger.info(f"yt-dlp stream extraction succeeded for: {url}")
                        return stream_url, title, is_live, duration, http_headers
            except Exception as e:
                logger.warning(f"yt-dlp extract_stream_url_only failed: {e}")
        return None

    @classmethod
    async def extract_stream_url_async(cls, url: str) -> Optional[tuple[str, str, bool, int, Optional[dict]]]:
        """Extract stream URL concurrently racing yt-dlp (with cookies) and fast public fallbacks for sub-second response times."""
        if not YouTubeHandler.is_playable_youtube_url(url):
            logger.info("Rejected non-playable YouTube URL during stream extraction: %s", url)
            return None

        # 1. Start primary yt-dlp extraction. The fallbacks race alongside and
        # win whenever they are faster, so start latency is decided by the
        # race, NOT by this timeout -- it exists only as a safety net so a
        # wedged extraction cannot hang playback forever. Keep it comfortably
        # above yt-dlp's normal latency: capping below that made yt-dlp lose
        # every race and forced playback onto the public instances, which fail
        # outright when they are all down or rate-limited.
        async def _yt_dlp_task():
            try:
                return await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        _YDL_EXECUTOR, cls._ydl_extract_stream_url_only, url
                    ),
                    timeout=_YDL_EXTRACT_TIMEOUT
                )
            except asyncio.TimeoutError:
                # NOTE: wait_for cannot cancel a thread already running in the
                # executor -- it keeps going until yt-dlp returns. That is why
                # _YDL_EXECUTOR is dedicated and bounded: abandoned extractions
                # can no longer accumulate in the default pool and starve every
                # other blocking call the bot makes.
                logger.warning(
                    "yt-dlp extraction exceeded %.1fs for %s; relying on fallback",
                    _YDL_EXTRACT_TIMEOUT, url
                )
                return None
            except Exception as e:
                logger.warning(f"yt-dlp race task failed: {e}")
                return None

        # 2. Start fallback extraction concurrently
        ydl_task = asyncio.create_task(_yt_dlp_task())
        fallback_task = asyncio.create_task(cls._extract_stream_url_fallback_async(url))
        
        pending = {ydl_task, fallback_task}
        result = None
        
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    res = task.result()
                    if res:
                        result = res
                        break
                except Exception:
                    pass
            if result:
                break
                
        for task in pending:
            task.cancel()
            
        return result


    @classmethod
    def extract_stream_url(cls, url: str) -> Optional[tuple[str, str, bool, int, Optional[dict]]]:
        """Synchronous wrapper for stream extraction.
        
        Kept for backward compat with code that calls this from a thread executor.
        """
        return cls._ydl_extract_stream_url(url)

    @classmethod
    def download_audio(cls, url: str) -> Optional[str]:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".m4a")
        tmp_name = tmp.name
        tmp.close()  # Close the file so yt-dlp can write to it
        
        ydl_opts = get_ydl_opts({
            'format': 'bestaudio/best',
            'outtmpl': tmp_name,
            'quiet': True,
            'no_warnings': True,
            'overwrites': True,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                ydl.download([url])
                return tmp_name
            except Exception as e:
                logger.error(f"Failed to download YouTube audio: {e}")
            return None

    @classmethod
    async def _download_stream_to_tempfile(cls, stream_url: str, max_bytes: int = 250 * 1024 * 1024) -> Optional[str]:
        try:
            import httpx
            import tempfile
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                async with client.stream("GET", stream_url, headers=headers) as resp:
                    if resp.status_code != 200:
                        logger.error(f"Failed to download YouTube stream: HTTP {resp.status_code}")
                        return None
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                    tmp_name = tmp.name
                    total = 0
                    try:
                        with tmp as f:
                            async for chunk in resp.aiter_bytes(chunk_size=128 * 1024):
                                f.write(chunk)
                                total += len(chunk)
                                if total > max_bytes:
                                    break
                        # Reject downloads that are empty or too small (less than 10 KB)
                        if total < 10000 or total > max_bytes:
                            try:
                                os.remove(tmp_name)
                            except Exception:
                                pass
                            logger.error(f"Downloaded file too small or exceeded limit: {total} bytes")
                            return None
                        return tmp_name
                    except Exception as e:
                        try:
                            os.remove(tmp_name)
                        except Exception:
                            pass
                        raise e
        except Exception as e:
            logger.error(f"YouTube download stream error: {e}")
            return None

    @classmethod
    async def get_audio_source(cls, query: str, volume: float = 1.0, effects: Optional[list[str]] = None, seek: Optional[float] = None, eq_preset: Optional[str] = None):
        try:
            if cls.is_youtube_url(query):
                meta = await cls.extract_stream_url_async(query)
                if not meta:
                    return None, None
                stream_url, title, is_live, duration, *extra = meta
                http_headers = extra[0] if extra else None
                
                ffmpeg_options = build_ffmpeg_options(effects, eq_preset)

                try:
                    source_path = stream_url
                    http_before = build_http_before_options(http_headers)
                    ffmpeg_before_options = apply_seek(http_before, seek)
                    source = make_stream_source(source_path, ffmpeg_before_options, ffmpeg_options)
                    return source, title
                except Exception as fe:
                    logger.warning(f"Direct stream creation failed: {fe}. Falling back to tempfile download...")
                    temp_path = await cls._download_stream_to_tempfile(stream_url)
                    if temp_path:
                        source = make_stream_source(
                            temp_path,
                            apply_seek(_FILE_BEFORE_OPTIONS, seek),
                            ffmpeg_options,
                            temp_file_path=temp_path,
                        )
                        return source, title
                    return None, None
            else:
                results = await cls.search(query, limit=1)
                if not results:
                    return None, None
                return await cls.get_audio_source_for_song(results[0], volume, effects, seek, eq_preset)
        except Exception as e:
            logger.error(f"YouTube get_audio_source error: {e}")
            return None, None

    @classmethod
    async def get_audio_source_for_song(cls, song: dict, volume: float = 1.0, effects: Optional[list[str]] = None, seek: Optional[float] = None, eq_preset: Optional[str] = None):
        try:
            url = song.get("url") or song.get("id")
            if not url:
                return None, None
            
            meta = await cls.extract_stream_url_async(url)
            if not meta:
                return None, None
            
            stream_url, title, is_live, duration, *extra = meta
            http_headers = extra[0] if extra else None
            # When a public fallback (Invidious/Cobalt) wins the extraction race
            # it may not carry a title; prefer the search-result title then.
            if not title or title == "Unknown YouTube Video":
                title = song.get("title") or song.get("song") or title
            
            ffmpeg_options = build_ffmpeg_options(effects, eq_preset)

            try:
                source_path = stream_url
                http_before = build_http_before_options(http_headers)
                ffmpeg_before_options = apply_seek(http_before, seek)
                source = make_stream_source(source_path, ffmpeg_before_options, ffmpeg_options)
                return source, title
            except Exception as fe:
                logger.warning(f"Direct stream creation failed: {fe}. Falling back to tempfile download...")
                temp_path = await cls._download_stream_to_tempfile(stream_url)
                if temp_path:
                    source = make_stream_source(
                        temp_path,
                        apply_seek(_FILE_BEFORE_OPTIONS, seek),
                        ffmpeg_options,
                        temp_file_path=temp_path,
                    )
                    return source, title
                return None, None
        except Exception as e:
            logger.error(f"YouTube get_audio_source_for_song error: {e}")
            return None, None

    @classmethod
    def extract_playlist_videos(cls, url: str) -> Optional[list[dict]]:
        ydl_opts = get_ydl_opts({
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
        })
        if 'noplaylist' in ydl_opts:
            del ydl_opts['noplaylist']
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                if 'entries' in info:
                    return [
                        {
                            'url': f"https://www.youtube.com/watch?v={entry.get('id')}",
                            'title': entry.get('title') or 'Unknown YouTube Video'
                        }
                        for entry in info['entries'] if entry and entry.get('id')
                    ]
                return None
            except Exception as e:
                logger.error(f"Failed to extract playlist videos: {e}")
                return None
