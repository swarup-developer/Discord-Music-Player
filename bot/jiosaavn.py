import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx

import discord
from Crypto.Cipher import DES
from Crypto.Util.Padding import unpad

logger = logging.getLogger(__name__)
HAS_CRYPTO = True


class JioSaavnEngine:
    AUTOCOMPLETE_URL = "https://www.jiosaavn.com/api.php?__call=autocomplete.get&_format=json&_marker=0&cc=in&includeMetaTags=1&query="
    SONG_SEARCH_URL = "https://www.jiosaavn.com/api.php?__call=search.getResults&_format=json&_marker=0&cc=in&includeMetaTags=1&n=30&p=1&q="
    SONG_DETAILS_URL = "https://www.jiosaavn.com/api.php?__call=song.getDetails&cc=in&_marker=0%3F_marker%3D0&_format=json&pids="
    DES_KEY = b"38346591"

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )

    def format_string(self, text: str) -> str:
        if not text:
            return ""
        return text.replace("&quot;", "'").replace("&amp;", "&").replace("&#039;", "'")

    def normalize_string(self, s: str) -> str:
        if not s:
            return ""
        s = s.lower()
        s = re.sub(r"[^a-z0-9]", "", s)
        s = re.sub(r"(.)\1+", r"\1", s)
        return s

    def decrypt_url(self, encrypted_url: str, quality: str = "320") -> Optional[str]:
        if not encrypted_url or not HAS_CRYPTO:
            return None
        try:
            cipher = DES.new(self.DES_KEY, DES.MODE_ECB)
            encrypted_data = base64.b64decode(encrypted_url.strip())
            decrypted_text = unpad(cipher.decrypt(encrypted_data), DES.block_size).decode("utf-8")
            return (
                decrypted_text.replace("_96.mp4", f"_{quality}.mp4")
                .replace("_96_p.mp4", f"_{quality}.mp4")
                .replace("_160.mp4", f"_{quality}.mp4")
            )
        except Exception as e:
            logger.error(f"Decryption error: {e}")
            return None

    async def search(self, query: str, page: int = 1) -> List[Dict[str, Any]]:
        try:
            search_url = self.SONG_SEARCH_URL + urllib.parse.quote(query) + f"&p={page}"
            autocomplete_url = self.AUTOCOMPLETE_URL + urllib.parse.quote(query)
            tasks = [self.client.get(search_url)]
            if page == 1:
                tasks.append(self.client.get(autocomplete_url))
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            results = []
            if page == 1 and len(responses) > 1 and not isinstance(responses[1], Exception) and responses[1].status_code == 200:
                try:
                    data = json.loads(re.sub(r'\(From "([^"]+)"\)', r"(From '\1')", responses[1].text))
                    results.extend(data.get("songs", {}).get("data", []))
                except Exception:
                    pass
            if not isinstance(responses[0], Exception) and responses[0].status_code == 200:
                try:
                    results.extend(responses[0].json().get("results", []))
                except Exception:
                    pass
            unique = {}
            for song in results:
                if song.get("id"):
                    song["provider"] = "jiosaavn"
                    unique[song["id"]] = song
            return list(unique.values())
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    async def get_song_details(self, song_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = await self.client.get(self.SONG_DETAILS_URL + song_id)
            return resp.json().get(song_id) if resp.status_code == 200 else None
        except Exception as e:
            logger.error(f"Get details error: {e}")
            return None

    async def get_media_url(self, song: Dict[str, Any]) -> Optional[str]:
        more_info = song.get("more_info", {})
        encrypted_url = song.get("encrypted_media_url") or more_info.get("encrypted_media_url")
        if encrypted_url:
            decrypted = self.decrypt_url(encrypted_url)
            if decrypted:
                return decrypted
        media_url = song.get("media_url") or more_info.get("media_url")
        if media_url:
            return media_url
        preview_url = song.get("media_preview_url") or more_info.get("media_preview_url")
        if preview_url:
            url = preview_url.replace("preview", "aac")
            is_320 = str(song.get("320kbps") or more_info.get("320kbps")).lower() == "true"
            return url.replace("_96_p.mp4", "_320.mp4" if is_320 else "_160.mp4")
        return None

    async def get_song_details_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        try:
            url = f"https://www.jiosaavn.com/api.php?__call=webapi.get&_format=json&_marker=0&cc=in&includeMetaTags=1&token={token}&type=song"
            resp = await self.client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, dict):
                    if data.get("status") == "failure":
                        return None
                    for val in data.values():
                        if isinstance(val, dict) and "id" in val:
                            return val
            return None
        except Exception as e:
            logger.error(f"Get details by token error: {e}")
            return None


class JioSaavnHandler:
    engine = JioSaavnEngine()

    @staticmethod
    def is_jiosaavn_url(query: str) -> bool:
        return "jiosaavn.com" in query.lower() or "saavn" in query.lower()

    @classmethod
    def extract_token_from_url(cls, url: str) -> Optional[str]:
        # Strip trailing slashes and split
        parts = url.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] == "song":
            return parts[-1]
        try:
            idx = parts.index("song")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        except ValueError:
            pass
        return None

    @classmethod
    async def _download_stream_to_tempfile(cls, stream_url: str, max_bytes: int = 250 * 1024 * 1024) -> Optional[str]:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.jiosaavn.com/",
                "Accept": "*/*",
                "Origin": "https://www.jiosaavn.com",
                "Connection": "keep-alive",
            }
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                async with client.stream("GET", stream_url, headers=headers) as resp:
                    if resp.status_code != 200:
                        return None
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                    total = 0
                    tmp_name = tmp.name
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
            logger.error(f"Download error: {e}")
            return None

    @classmethod
    async def get_audio_source(cls, query: str, volume: float = 1.0, effects: Optional[list[str]] = None, seek: Optional[float] = None, eq_preset: Optional[str] = None):
        try:
            song_data = None
            if cls.is_jiosaavn_url(query):
                token = cls.extract_token_from_url(query)
                if token:
                    song_data = await cls.engine.get_song_details_by_token(token)
            
            if not song_data:
                results = await cls.engine.search(query)
                if not results:
                    return None, None
                song_data = results[0]
                
            song_id = song_data.get("id")
            full_data = await cls.engine.get_song_details(song_id)
            if full_data:
                song_data.update(full_data)
            media_url = await cls.engine.get_media_url(song_data)
            if not media_url:
                return None, None
            
            # Download stream to local temporary file to prevent buffering lag
            temp_path = await cls._download_stream_to_tempfile(media_url)
            if not temp_path:
                return None, None
                
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
            
            source = discord.FFmpegPCMAudio(temp_path, before_options=ffmpeg_before_options, options=ffmpeg_options)
            source.temp_file_path = temp_path
            return source, cls.engine.format_string(song_data.get("song") or song_data.get("title") or "Unknown")
        except Exception as e:
            logger.error(f"Audio source error: {e}")
            return None, None

    @classmethod
    async def get_audio_source_for_song(cls, song: dict, volume: float = 1.0, effects: Optional[list[str]] = None, seek: Optional[float] = None, eq_preset: Optional[str] = None):
        try:
            song_data = dict(song)
            song_id = song_data.get("id")
            if song_id:
                full_data = await cls.engine.get_song_details(song_id)
                if full_data:
                    song_data.update(full_data)
            media_url = await cls.engine.get_media_url(song_data)
            if not media_url:
                return None, None

            # Download stream to local temporary file to prevent buffering lag
            temp_path = await cls._download_stream_to_tempfile(media_url)
            if not temp_path:
                return None, None

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

            source = discord.FFmpegPCMAudio(temp_path, before_options=ffmpeg_before_options, options=ffmpeg_options)
            source.temp_file_path = temp_path
            return source, cls.engine.format_string(song_data.get("song") or song_data.get("title") or "Unknown")
        except Exception as e:
            logger.error(f"Audio source error: {e}")
            return None, None
