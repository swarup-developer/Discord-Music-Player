import asyncio
import json
import logging
import os
import re
import tempfile
import urllib.request
from typing import Optional, Any

import discord
import yt_dlp

logger = logging.getLogger(__name__)

  # seconds per instance attempt

# yt-dlp fallback config
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',  # Bind to IPv4 to prevent IPv6 timeout issues
    'remote_components': ['ejs:github'],
    'nocache': True,
}


def get_ydl_opts(extra_opts: Optional[dict] = None) -> dict:
    opts = {**YDL_OPTIONS}
    if extra_opts:
        opts.update(extra_opts)
        
    # Check for cookies.txt in current working directory or bot directory
    cookie_path = os.path.join(os.getcwd(), 'cookies.txt')
    if os.path.exists(cookie_path):
        opts['cookiefile'] = cookie_path
    else:
        # Check bot directory as fallback
        fallback_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cookies.txt')
        if os.path.exists(fallback_path):
            opts['cookiefile'] = fallback_path
            
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


# Invidious public instances list for fallback extraction
INVIDIOUS_INSTANCES = [
    "inv.thepixora.com",
    "yt.chocolatemoo53.com",
    "invidious.nerdvpn.de",
    "invidious.f5.si",
    "invidious.tiekoetter.com",
    "inv.nadeko.net"
]

# Cobalt public instances list for fallback extraction
COBALT_INSTANCES = [
    "https://api.cobalt.tools/api/json",
    "https://cobalt.api.ryder.rip/api/json",
    "https://co.wuk.sh/api/json",
    "https://api.wuk.sh/api/json"
]


class YouTubeHandler:
    _working_invidious_instance = None
    _working_cobalt_instance = None

    @classmethod
    def _get_invidious_instances(cls) -> list[str]:
        instances = list(INVIDIOUS_INSTANCES)
        if cls._working_invidious_instance and cls._working_invidious_instance in instances:
            instances.remove(cls._working_invidious_instance)
            instances.insert(0, cls._working_invidious_instance)
        return instances

    @classmethod
    def _get_cobalt_instances(cls) -> list[str]:
        instances = list(COBALT_INSTANCES)
        if cls._working_cobalt_instance and cls._working_cobalt_instance in instances:
            instances.remove(cls._working_cobalt_instance)
            instances.insert(0, cls._working_cobalt_instance)
        return instances
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
        """Search YouTube videos using Invidious API."""
        import urllib.parse
        encoded_query = urllib.parse.quote(query)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        instances = cls._get_invidious_instances()
        for instance in instances:
            api_url = f"https://{instance}/api/v1/search?q={encoded_query}&type=video"
            try:
                def _fetch():
                    req = urllib.request.Request(api_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return json.loads(response.read().decode())
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(None, _fetch)
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
                logger.warning(f"Invidious search failed on {instance}: {e}")
        return []

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
    async def search(cls, query: str, limit: int = 10) -> list[dict]:
        # For URLs, don't use search — go straight to yt-dlp
        if query.startswith("http://") or query.startswith("https://"):
            if YouTubeHandler.is_youtube_url(query) and not YouTubeHandler.is_playable_youtube_url(query):
                logger.info("Rejected non-playable YouTube URL during search: %s", query)
                return []
            return await cls._ydl_search(query, limit)

        # Try official YouTube API search first
        api_results = await cls._youtube_api_search(query, limit)
        if api_results:
            return api_results

        # Try Invidious search first as a cookie-free fallback
        invidious_results = await cls._invidious_search(query, limit)
        if invidious_results:
            return invidious_results

        return await cls._ydl_search(query, limit)

    @classmethod
    async def _ydl_search(cls, query: str, limit: int = 10) -> list[dict]:
        """yt-dlp-based search. """
        def _search():
            ydl_opts = get_ydl_opts({'playlist_items': f'1-{limit}'})
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
    def _extract_via_cobalt(cls, url: str) -> Optional[str]:
        """Fetch audio stream from Cobalt instances."""
        data = {
            "url": url,
            "downloadMode": "audio",
            "audioFormat": "mp3"
        }
        payload = json.dumps(data).encode('utf-8')
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Origin': 'https://cobalt.tools',
            'Referer': 'https://cobalt.tools/'
        }
        instances = cls._get_cobalt_instances()
        for instance_url in instances:
            try:
                req = urllib.request.Request(
                    instance_url,
                    data=payload,
                    headers=headers,
                    method='POST'
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    res_data = json.loads(response.read().decode())
                    if res_data.get("url"):
                        cls._working_cobalt_instance = instance_url
                        return res_data.get("url")
            except Exception as e:
                logger.warning(f"Cobalt instance {instance_url} failed: {e}")
        return None

    @classmethod
    def _extract_via_invidious(cls, video_id: str) -> Optional[tuple[str, str, bool, int]]:
        """Fetch audio stream and metadata from Invidious instances."""
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        instances = cls._get_invidious_instances()
        for instance in instances:
            api_url = f"https://{instance}/api/v1/videos/{video_id}"
            try:
                req = urllib.request.Request(api_url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode())
                    title = data.get("title") or "Unknown YouTube Video"
                    is_live = data.get("liveNow", False)
                    duration = data.get("lengthSeconds", 0)
                    
                    # Construct a proxied stream URL to bypass YouTube IP restrictions (no 403 Forbidden)
                    stream_url = f"https://{instance}/latest_version?id={video_id}&itag=140&local=true"
                    cls._working_invidious_instance = instance
                    return stream_url, title, is_live, duration
            except Exception as e:
                logger.warning(f"Invidious instance {instance} failed: {e}")
        return None

    @classmethod
    def _extract_stream_url_fallback(cls, url: str) -> Optional[tuple[str, str, bool, int]]:
        """Try Invidious first (proxied), then Cobalt (direct), and return (stream_url, title, is_live, duration)."""
        video_id = _extract_video_id(url)
        if not video_id:
            return None

        # 1. Try Invidious first (returns a fully proxied stream URL to bypass 403 Forbidden)
        logger.info(f"Attempting Invidious fallback for video: {video_id}")
        invidious_res = cls._extract_via_invidious(video_id)
        if invidious_res:
            logger.info(f"Using Invidious proxied stream and metadata for {video_id}")
            return invidious_res

        # 2. If Invidious failed completely, try Cobalt direct (with empty metadata)
        logger.info(f"Attempting direct Cobalt extraction for video: {video_id}")
        cobalt_stream_url = cls._extract_via_cobalt(url)
        if cobalt_stream_url:
            return cobalt_stream_url, "Unknown YouTube Video", False, 0

        return None

    @classmethod
    def _ydl_extract_stream_url(cls, url: str) -> Optional[tuple[str, str, bool, int]]:
        """Extract stream URL using Cobalt/Invidious fallbacks, with yt-dlp as final fallback. Blocking."""
        fallback = cls._extract_stream_url_fallback(url)
        if fallback:
            return fallback

        logger.info("Falling back to yt-dlp for stream extraction...")
        ydl_opts = get_ydl_opts({
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                stream_url = info.get('url')
                title = info.get('title', 'Unknown YouTube Video')
                is_live = info.get('is_live', False) or info.get('live_status') == 'is_live'
                duration = info.get('duration', 0)
                return stream_url, title, is_live, duration
            except Exception as e:
                logger.error(f"yt-dlp extract_stream_url failed: {e}")
                return None

    @classmethod
    async def extract_stream_url_async(cls, url: str) -> Optional[tuple[str, str, bool, int]]:
        """Direct yt-dlp stream extraction."""
        if not YouTubeHandler.is_playable_youtube_url(url):
            logger.info("Rejected non-playable YouTube URL during stream extraction: %s", url)
            return None
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cls._ydl_extract_stream_url, url)

    @classmethod
    def extract_stream_url(cls, url: str) -> Optional[tuple[str, str, bool, int]]:
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
    async def get_audio_source(cls, query: str, volume: float = 1.0, effects: Optional[list[str]] = None, seek: Optional[float] = None, eq_preset: Optional[str] = None):
        try:
            if cls.is_youtube_url(query):
                meta = await cls.extract_stream_url_async(query)
                if not meta:
                    return None, None
                stream_url, title, is_live, duration = meta
                source_path = stream_url
                ffmpeg_before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
                if seek and seek > 0:
                    ffmpeg_before_options = f"-ss {seek} " + ffmpeg_before_options
                
                ffmpeg_options = "-vn"
                filters = []
                if eq_preset:
                    if eq_preset == "bassboost":
                        filters.append("equalizer=f=60:width_type=o:width=2:g=8")
                    elif eq_preset == "vocal":
                        filters.append("equalizer=f=1000:width_type=q:width=1:g=5,equalizer=f=3000:width_type=q:width=1:g=3")
                    elif eq_preset == "treble":
                        filters.append("equalizer=f=8000:width_type=o:width=2:g=6")
                    elif eq_preset == "lofi":
                        filters.append("highpass=f=300,lowpass=f=4000")
                    elif eq_preset == "acoustic":
                        filters.append("equalizer=f=120:width_type=o:width=2:g=3,equalizer=f=2000:width_type=o:width=2:g=2,equalizer=f=8000:width_type=o:width=2:g=3")

                if effects:
                    if "bassboost" in effects and eq_preset != "bassboost":
                        filters.append("equalizer=f=60:width_type=o:width=2:g=8")
                    if "nightcore" in effects:
                        filters.append("asetrate=48000*1.25")
                filters.append("aresample=resampler=soxr:osr=48000:osf=s16")
                ffmpeg_options += f' -af "{",".join(filters)}"'
                source = discord.FFmpegPCMAudio(source_path, before_options=ffmpeg_before_options, options=ffmpeg_options)
                return source, title
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
            
            # Use the async Piped-first extraction
            meta = await cls.extract_stream_url_async(url)
            if not meta:
                return None, None
            
            stream_url, title, is_live, duration = meta
            
            source_path = stream_url
            ffmpeg_before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
            if seek and seek > 0:
                ffmpeg_before_options = f"-ss {seek} " + ffmpeg_before_options
            
            ffmpeg_options = "-vn"
            filters = []
            if eq_preset:
                if eq_preset == "bassboost":
                    filters.append("equalizer=f=60:width_type=o:width=2:g=8")
                elif eq_preset == "vocal":
                    filters.append("equalizer=f=1000:width_type=q:width=1:g=5,equalizer=f=3000:width_type=q:width=1:g=3")
                elif eq_preset == "treble":
                    filters.append("equalizer=f=8000:width_type=o:width=2:g=6")
                elif eq_preset == "lofi":
                    filters.append("highpass=f=300,lowpass=f=4000")
                elif eq_preset == "acoustic":
                    filters.append("equalizer=f=120:width_type=o:width=2:g=3,equalizer=f=2000:width_type=o:width=2:g=2,equalizer=f=8000:width_type=o:width=2:g=3")

            if effects:
                if "bassboost" in effects and eq_preset != "bassboost":
                    filters.append("equalizer=f=60:width_type=o:width=2:g=8")
                if "nightcore" in effects:
                    filters.append("asetrate=48000*1.25")
            
            # Use high-quality soxr resampling to convert back to 48kHz PCM
            filters.append("aresample=resampler=soxr:osr=48000:osf=s16")
            ffmpeg_options += f' -af "{",".join(filters)}"'

            source = discord.FFmpegPCMAudio(source_path, before_options=ffmpeg_before_options, options=ffmpeg_options)
            return source, title
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
                            'url': entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}",
                            'title': entry.get('title') or 'Unknown YouTube Video'
                        }
                        for entry in info['entries'] if entry
                    ]
                return None
            except Exception as e:
                logger.error(f"Failed to extract playlist videos: {e}")
                return None