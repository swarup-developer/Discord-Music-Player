import asyncio
import base64
import json
import logging
import os
import re
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
    # NOTE: search.getResults is intentionally removed — it stopped returning
    # results on JioSaavn's side as of mid-2026. Search now uses autocomplete.get
    # + song.getDetails instead (see search() method).
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
        """Search songs via autocomplete.get + song.getDetails batch fetch.

        NOTE: search.getResults (the old primary endpoint) now always returns
        empty results on JioSaavn's side (broken as of mid-2026). We fall back
        to autocomplete.get, which still works, and resolve song PIDs from its
        topquery and albums entries, then enrich them with song.getDetails.
        """
        try:
            base_url = self.AUTOCOMPLETE_URL + urllib.parse.quote(query)
            base_resp = await self.client.get(base_url)

            def _parse_autocomplete_data(data: dict) -> List[str]:
                """Return ordered list of song PIDs from a parsed autocomplete payload."""
                pids: List[str] = []
                topquery_dict = data.get("topquery") or {}
                topquery_data = topquery_dict.get("data") or []
                for item in topquery_data:
                    if not item:
                        continue
                    # If topquery item is a song, its PID is in item["id"] directly.
                    # If it's an album, PIDs are in more_info.song_pids.
                    if item.get("type") == "song":
                        p = item.get("id", "").strip()
                        if p and p not in pids:
                            pids.append(p)
                    else:
                        more_info = item.get("more_info") or {}
                        for p in more_info.get("song_pids", "").split(","):
                            p = p.strip()
                            if p and p not in pids:
                                pids.append(p)
                
                songs_dict = data.get("songs") or {}
                songs_data = songs_dict.get("data") or []
                for item in songs_data:
                    if not item:
                        continue
                    p = item.get("id", "").strip()
                    if p and p not in pids:
                        pids.append(p)
                
                albums_dict = data.get("albums") or {}
                albums_data = albums_dict.get("data") or []
                albums = sorted(
                    albums_data,
                    key=lambda a: int(a.get("ctr") or 0) if a else 0,
                    reverse=True,
                )
                for album in albums[:3]:
                    if not album:
                        continue
                    more_info = album.get("more_info") or {}
                    for p in more_info.get("song_pids", "").split(","):
                        p = p.strip()
                        if p and p not in pids:
                            pids.append(p)
                return pids

            def _parse_autocomplete(resp) -> tuple:
                """Parse an autocomplete HTTP response. Returns (pids, data_dict)."""
                if isinstance(resp, Exception) or resp.status_code != 200:
                    return [], {}
                try:
                    data = json.loads(re.sub(r'\(From "([^"]+)"\)', r"(From '\1')", resp.text))
                except Exception:
                    return [], {}
                return _parse_autocomplete_data(data), data            # Fire base query AND a 'hindi' disambiguation in parallel.
            # The ' hindi' suffix reliably makes JioSaavn surface the original
            # Bollywood song even when the bare query returns only lofi/regional
            # variants (confirmed empirically: 'Kesariya hindi' → Brahmastra original).
            # Both requests fire together so there is zero extra wall-clock time.
            base_resp, hindi_resp = await asyncio.gather(
                self.client.get(base_url),
                self.client.get(self.AUTOCOMPLETE_URL + urllib.parse.quote(f"{query} hindi")),
                return_exceptions=True,
            )

            base_pids, _ = _parse_autocomplete(base_resp)
            hindi_pids, _ = _parse_autocomplete(hindi_resp)

            # Merge: base first, then anything new from the hindi pass
            merged: List[str] = list(base_pids)
            for p in hindi_pids:
                if p not in merged:
                    merged.append(p)

            ordered_pids = merged[:30]
            if not ordered_pids:
                logger.info(f"No song PIDs found via autocomplete for: {query}")
                return []

            # --- Batch-fetch song details (song.getDetails accepts comma-separated pids) ---
            pids_str = ",".join(ordered_pids)
            details_resp = await self.client.get(self.SONG_DETAILS_URL + urllib.parse.quote(pids_str))
            if details_resp.status_code != 200:
                logger.warning(f"song.getDetails returned {details_resp.status_code}")
                return []

            try:
                details_data = details_resp.json()
            except Exception as e:
                logger.error(f"song.getDetails JSON parse error: {e}")
                return []

            results = []
            seen: set = set()
            for pid in ordered_pids:
                song = details_data.get(pid)
                if song and isinstance(song, dict) and song.get("id") and song["id"] not in seen:
                    song["provider"] = "jiosaavn"
                    results.append(song)
                    seen.add(song["id"])

            return self._rank_results(results, query)
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    # Keywords that identify non-original versions of a song.
    # Songs whose titles contain any of these (and whose query does NOT) are
    # pushed to the bottom of search results so the original always plays first.
    _NON_ORIGINAL_KEYWORDS = {
        "lofi", "lo-fi", "slowed", "reverb", "remix", "karaoke", "cover",
        "flip", "reprise", "mashup", "tribute", "instrumental", "unplugged",
        "acoustic version", "extended", "8d audio", "432hz", "sped up",
    }

    def _rank_results(self, results: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Sort search results so the most relevant original song comes first.

        Tier 0 — title contains ALL query words (direct match, e.g. "Kesariya" in title)
        Tier 1 — title contains SOME query words (partial match)
        Tier 2 — title contains NONE of the query words (unrelated, from album fallback)
        Tier 3 — non-original version (lofi / slowed / remix) unless query asked for it

        Within each tier, play_count descending breaks ties.
        """
        query_lower = query.lower()
        query_wants_remix = any(kw in query_lower for kw in self._NON_ORIGINAL_KEYWORDS)
        # Meaningful query words (strip short stop words)
        query_words = [w for w in re.split(r"\s+", query_lower) if len(w) > 2]

        def _score(song: Dict[str, Any]) -> tuple:
            title = (song.get("song") or song.get("title") or "").lower()
            plays = int(song.get("play_count") or 0)

            is_non_original = (
                not query_wants_remix
                and any(kw in title for kw in self._NON_ORIGINAL_KEYWORDS)
            )
            if is_non_original:
                return (3, -plays)

            if query_words:
                matched = sum(1 for w in query_words if w in title)
                if matched == len(query_words):
                    tier = 0  # full match
                elif matched > 0:
                    tier = 1  # partial match
                else:
                    tier = 2  # unrelated (album-spillover songs)
            else:
                tier = 1

            return (tier, -plays)

        return sorted(results, key=_score)

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

    @staticmethod
    def _build_ffmpeg_options(
        seek: Optional[float],
        effects: Optional[list],
        eq_preset: Optional[str],
        is_http: bool = False,
    ) -> tuple:
        """Build (before_options, options) strings for FFmpegPCMAudio.

        When streaming directly from an HTTP URL (`is_http=True`) we add
        reconnect flags so FFmpeg can recover from transient CDN hiccups.
        """
        before_parts = ["-nostdin"]
        if is_http:
            # Let FFmpeg reconnect if the CDN drops the connection mid-stream.
            before_parts += [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-reconnect_at_eof", "1",
            ]
        if seek and seek > 0:
            before_parts = [f"-ss {seek}"] + before_parts

        filters = []
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

        options = f'-vn -flags low_delay -af "{",".join(filters)}"'
        return " ".join(before_parts), options

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
                # search() already returns full song.getDetails data — no extra
                # HTTP call needed here.
                song_data = results[0]
            else:
                # URL lookup path: enrich with full details if not already present.
                song_id = song_data.get("id")
                if song_id:
                    full_data = await cls.engine.get_song_details(song_id)
                    if full_data:
                        song_data.update(full_data)

            media_url = await cls.engine.get_media_url(song_data)
            if not media_url:
                return None, None

            # Stream directly from CDN — no temp-file download needed.
            before_opts, opts = cls._build_ffmpeg_options(seek, effects, eq_preset, is_http=True)
            source = discord.FFmpegPCMAudio(media_url, before_options=before_opts, options=opts)
            source.temp_file_path = None  # No temp file; cleanup in cogs_music is safe.
            title = cls.engine.format_string(song_data.get("song") or song_data.get("title") or "Unknown")
            return source, title
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

            # Stream directly from CDN — no temp-file download needed.
            before_opts, opts = cls._build_ffmpeg_options(seek, effects, eq_preset, is_http=True)
            source = discord.FFmpegPCMAudio(media_url, before_options=before_opts, options=opts)
            source.temp_file_path = None  # No temp file; cleanup in cogs_music is safe.
            title = cls.engine.format_string(song_data.get("song") or song_data.get("title") or "Unknown")
            return source, title
        except Exception as e:
            logger.error(f"Audio source error: {e}")
            return None, None
