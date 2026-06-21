import asyncio
import logging
import os
import re
import tempfile
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
    async def search(cls, query: str, limit: int = 10) -> list[dict]:
        # For URLs, don't use search — go straight to yt-dlp
        if query.startswith("http://") or query.startswith("https://"):
            if YouTubeHandler.is_youtube_url(query) and not YouTubeHandler.is_playable_youtube_url(query):
                logger.info("Rejected non-playable YouTube URL during search: %s", query)
                return []
            return await cls._ydl_search(query, limit)

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
    def _ydl_extract_stream_url(cls, url: str) -> Optional[tuple[str, str, bool, int]]:
        """Original yt-dlp stream extraction. Blocking."""
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

    def extract_stream_url(cls, url: str) -> Optional[tuple[str, str, bool, int]]:
        """Synchronous wrapper — runs Piped-first async extraction in a new loop if needed.
        
        Kept for backward compat with code that calls this from a thread executor.
        Falls back to pure yt-dlp if async is not available.
        """
        video_id = _extract_video_id(url)
        if YouTubeHandler.is_youtube_url(url) and not video_id and not _is_youtube_playlist_url(url):
            logger.info("Rejected non-playable YouTube URL during sync stream extraction: %s", url)
            return None
        if video_id:
            try:
                import httpx as _httpx
                with _httpx.Client(timeout=PIPED_TIMEOUT, follow_redirects=True) as client:
                    for inst in PIPED_INSTANCES:
                        api_url = f'https://{inst}/streams/{video_id}'
                        try:
                            r = client.get(api_url, headers={'User-Agent': 'Mozilla/5.0'})
                            if r.status_code == 200:
                                data = r.json()
                                audio_streams = data.get('audioStreams') or []
                                if audio_streams:
                                    best = max(audio_streams, key=lambda s: s.get('bitrate', 0))
                                    stream_url = best.get('url')
                                    if stream_url:
                                        title = data.get('title') or 'Unknown YouTube Video'
                                        is_live = data.get('livestream', False)
                                        duration = data.get('duration', 0)
                                        logger.info(f"Piped (sync) stream OK for {video_id}")
                                        return stream_url, title, is_live, duration
                        except Exception as e:
                            logger.warning(f"Piped (sync) {inst} failed: {e}")
            except Exception as e:
                logger.warning(f"Piped sync extraction error: {e}")

        # yt-dlp fallback
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
                try:
                    os.remove(tmp_name)
                except Exception:
                    pass
                return None

    @classmethod
    async def get_audio_source(cls, query: str, volume: float = 1.0, effects: Optional[list[str]] = None):
        try:
            results = await cls.search(query, limit=1)
            if not results:
                return None, None
            return await cls.get_audio_source_for_song(results[0], volume, effects)
        except Exception as e:
            logger.error(f"YouTube get_audio_source error: {e}")
            return None, None

    @classmethod
    async def get_audio_source_for_song(cls, song: dict, volume: float = 1.0, effects: Optional[list[str]] = None):
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
            
            ffmpeg_options = "-vn"
            filters = []
            if effects:
                if "bassboost" in effects:
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