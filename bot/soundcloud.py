import asyncio
import logging
import os
import tempfile
from typing import Optional, List, Dict, Any
import discord
import httpx
import yt_dlp

logger = logging.getLogger(__name__)

# Options for yt-dlp to query SoundCloud
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'scsearch',
    'source_address': '0.0.0.0',
    'nocache': True,
}

class SoundCloudHandler:
    @classmethod
    def is_soundcloud_url(cls, query: str) -> bool:
        return "soundcloud.com" in query.lower()

    @classmethod
    async def search(cls, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search SoundCloud using yt-dlp and return a list of song dictionaries.
        """
        loop = asyncio.get_event_loop()
        def _search():
            opts = {**YDL_OPTIONS, 'default_search': f'scsearch{limit}'}
            with yt_dlp.YoutubeDL(opts) as ydl:
                try:
                    info = ydl.extract_info(query, download=False)
                    if not info:
                        return []
                    
                    # If it's a search result, it contains an 'entries' list
                    if 'entries' in info:
                        entries = info['entries']
                    else:
                        entries = [info]

                    results = []
                    for entry in entries:
                        if not entry:
                            continue
                        results.append({
                            "title": entry.get("title", "Unknown"),
                            "song": entry.get("title", "Unknown"),
                            "album": entry.get("uploader") or "SoundCloud",
                            "url": entry.get("webpage_url") or entry.get("url"),
                            "duration": entry.get("duration"),
                            "id": entry.get("id"),
                            "uploader": entry.get("uploader", "Unknown"),
                            "thumbnail": entry.get("thumbnail"),
                            "provider": "soundcloud"
                        })
                    return results
                except Exception as e:
                    logger.error(f"SoundCloud search error: {e}")
                    return []
        
        return await loop.run_in_executor(None, _search)

    @classmethod
    async def extract_stream_url_async(cls, url: str) -> Optional[tuple[str, str, int]]:
        """
        Extract SoundCloud audio stream URL, title, and duration.
        """
        loop = asyncio.get_event_loop()
        def _extract():
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        return None
                    
                    stream_url = info.get("url")
                    title = info.get("title", "Unknown SoundCloud Track")
                    duration = info.get("duration", 0)
                    return stream_url, title, duration
                except Exception as e:
                    logger.error(f"SoundCloud extract error: {e}")
                    return None
                    
        return await loop.run_in_executor(None, _extract)

    @classmethod
    async def _download_stream_to_tempfile(cls, stream_url: str, max_bytes: int = 250 * 1024 * 1024) -> Optional[str]:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                async with client.stream("GET", stream_url, headers=headers) as resp:
                    if resp.status_code != 200:
                        logger.error(f"Failed to download SoundCloud stream: HTTP {resp.status_code}")
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
                        if total > max_bytes:
                            try:
                                os.remove(tmp_name)
                            except Exception:
                                pass
                            return None
                        return tmp_name
                    except Exception as e:
                        try:
                            os.remove(tmp_name)
                        except Exception:
                            pass
                        raise e
        except Exception as e:
            logger.error(f"SoundCloud download stream error: {e}")
            return None

    @classmethod
    async def get_audio_source(cls, query: str, volume: float = 1.0, effects: Optional[list[str]] = None, seek: Optional[float] = None, eq_preset: Optional[str] = None):
        try:
            if cls.is_soundcloud_url(query):
                meta = await cls.extract_stream_url_async(query)
                if not meta:
                    return None, None
                stream_url, title, duration = meta
                
                # Download stream to temporary local file to prevent buffering lag
                temp_path = await cls._download_stream_to_tempfile(stream_url)
                if not temp_path:
                    return None, None
                
                source_path = temp_path
                ffmpeg_before_options = "-nostdin -probesize 100000 -analyzeduration 100000 -fflags nobuffer"
                if seek and seek > 0:
                    ffmpeg_before_options = f"-ss {seek} " + ffmpeg_before_options
                
                ffmpeg_options = "-vn -flags low_delay"
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
                filters.append("aresample=osr=48000:osf=s16")
                ffmpeg_options += f' -af "{",".join(filters)}"'
                
                source = discord.FFmpegPCMAudio(source_path, before_options=ffmpeg_before_options, options=ffmpeg_options)
                source.temp_file_path = temp_path
                return source, title
            else:
                results = await cls.search(query, limit=1)
                if not results:
                    return None, None
                return await cls.get_audio_source_for_song(results[0], volume, effects, seek, eq_preset)
        except Exception as e:
            logger.error(f"SoundCloud get_audio_source error: {e}")
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
            
            stream_url, title, duration = meta
            temp_path = await cls._download_stream_to_tempfile(stream_url)
            if not temp_path:
                return None, None
            
            source_path = temp_path
            ffmpeg_before_options = "-nostdin -probesize 100000 -analyzeduration 100000 -fflags nobuffer"
            if seek and seek > 0:
                ffmpeg_before_options = f"-ss {seek} " + ffmpeg_before_options
            
            ffmpeg_options = "-vn -flags low_delay"
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
            filters.append("aresample=osr=48000:osf=s16")
            ffmpeg_options += f' -af "{",".join(filters)}"'
            
            source = discord.FFmpegPCMAudio(source_path, before_options=ffmpeg_before_options, options=ffmpeg_options)
            source.temp_file_path = temp_path
            return source, title
        except Exception as e:
            logger.error(f"SoundCloud get_audio_source_for_song error: {e}")
            return None, None
