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

from .audio_source import apply_seek, build_ffmpeg_options, make_stream_source

logger = logging.getLogger(__name__)
HAS_CRYPTO = True


class JioSaavnEngine:
    AUTOCOMPLETE_URL = "https://www.jiosaavn.com/api.php?__call=autocomplete.get&_format=json&_marker=0&cc=in&includeMetaTags=1&query="
    # search.getResults works again (verified Oct 2026) and returns full song
    # details -- including encrypted_media_url -- in a single request, ordered
    # by JioSaavn's own relevance. autocomplete.get stays as the fallback.
    SEARCH_URL = "https://www.jiosaavn.com/api.php?__call=search.getResults&_format=json&_marker=0&cc=in&p=1&n=20&q="
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
        """Search songs: search.getResults first, autocomplete as fallback."""
        try:
            resp = await self.client.get(self.SEARCH_URL + urllib.parse.quote(query))
            if resp.status_code == 200:
                results = [
                    {**song, "provider": "jiosaavn"}
                    for song in (resp.json().get("results") or [])
                    if isinstance(song, dict) and song.get("id")
                ]
                if results:
                    return self._rank_results(results, query)
            logger.info("search.getResults gave nothing for %r; using autocomplete", query)
        except Exception as e:
            logger.warning(f"search.getResults failed, using autocomplete: {e}")
        return await self._autocomplete_search(query)

    async def _autocomplete_search(self, query: str) -> List[Dict[str, Any]]:
        """Search songs via autocomplete.get + song.getDetails batch fetch.

        NOTE: search.getResults (the old primary endpoint) now always returns
        empty results on JioSaavn's side (broken as of mid-2026). We fall back
        to autocomplete.get, which still works, and resolve song PIDs from its
        topquery and albums entries, then enrich them with song.getDetails.
        """
        try:
            base_url = self.AUTOCOMPLETE_URL + urllib.parse.quote(query)

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
                return _parse_autocomplete_data(data), data

            # Fire base query AND a 'hindi' disambiguation in parallel.
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
        """Sort results so the right, current song comes first.

        Key, in order:
          1. tier: 0 exact title, 1 all query words in title, 2 some, 3 none,
             4 non-original (lofi/slowed/remix...) unless the query asked for it
          2. obscure: songs with < 5% of the tier's top play count go below
             established ones, so a fresh 2k-play cover cannot beat the hit
          3. newest year first -- new releases surface above older songs
             with the same title
          4. play count, then JioSaavn's own order (stable sort)
        """
        query_lower = query.lower().strip()
        query_wants_remix = any(kw in query_lower for kw in self._NON_ORIGINAL_KEYWORDS)
        query_words = [w for w in re.split(r"\s+", query_lower) if len(w) > 2]
        norm_query = self.normalize_string(query_lower)

        def _int(v) -> int:
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        def _title(song):
            return self.format_string(song.get("song") or song.get("title") or "").lower()

        def _tier(song) -> int:
            title = _title(song)
            if not query_wants_remix and any(kw in title for kw in self._NON_ORIGINAL_KEYWORDS):
                return 4
            base_title = re.sub(r"\s*\(.*?\)", "", title)
            if norm_query and self.normalize_string(base_title) == norm_query:
                return 0
            if not query_words:
                return 1
            matched = sum(1 for w in query_words if w in title)
            if matched == len(query_words):
                return 1
            return 2 if matched else 3

        # Labels re-upload the same recording on compilations every year
        # ("Tum Hi Ho (From 'Aashiqui 2')" 2026 ...). Collapse those to the
        # most-played copy so "newest first" ranks genuinely new songs, not
        # fresh re-uploads of an old one.
        def _identity(song):
            base = self.normalize_string(re.sub(r"\s*\(.*?\)", "", _title(song)))
            people = song.get("singers") or song.get("primary_artists") or ""
            if not people:
                more = song.get("more_info") or {}
                people = more.get("singers") or "" if isinstance(more, dict) else ""
            names = frozenset(self.normalize_string(n) for n in self.format_string(people).split(",") if n.strip())
            return base, names

        # Same base title + at least one shared singer = same song. Keep the
        # most-played copy; on a near tie (linked copies share play counts)
        # keep the oldest, i.e. the original album rather than a compilation.
        kept: List[tuple] = []  # (base, names, song)
        for song in results:
            base, names = _identity(song)
            for i, (kbase, knames, ksong) in enumerate(kept):
                if kbase == base and (names & knames or not names or not knames):
                    plays, kplays = _int(song.get("play_count")), _int(ksong.get("play_count"))
                    near_tie = abs(plays - kplays) <= max(plays, kplays) * 0.01
                    if (near_tie and _int(song.get("year")) < _int(ksong.get("year"))) or (not near_tie and plays > kplays):
                        kept[i] = (kbase, knames | names, song)
                    else:
                        kept[i] = (kbase, knames | names, ksong)
                    break
            else:
                kept.append((base, names, song))
        results = [k[2] for k in kept]

        tiers = {id(s): _tier(s) for s in results}
        top_plays: Dict[int, int] = {}
        for song in results:
            t = tiers[id(song)]
            top_plays[t] = max(top_plays.get(t, 0), _int(song.get("play_count")))

        def _score(song: Dict[str, Any]) -> tuple:
            t = tiers[id(song)]
            plays = _int(song.get("play_count"))
            obscure = plays < top_plays.get(t, 0) * 0.05
            return (t, obscure, -_int(song.get("year")), -plays)

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
        if not isinstance(more_info, dict):
            more_info = {}
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
        try:
            idx = parts.index("song")
            # The token is always the FINAL path segment. Links can be either
            # /song/<token> or /song/<slug>/<token>; returning parts[idx + 1]
            # would hand back the human-readable slug, which the API rejects.
            if idx + 1 < len(parts):
                return parts[-1]
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

        before = apply_seek(" ".join(before_parts), seek)
        return before, build_ffmpeg_options(effects, eq_preset)

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
                # URL lookup path: enrich with full details only when the token
                # lookup did not already return playable media info.
                song_id = song_data.get("id")
                if song_id and not cls._has_media(song_data):
                    full_data = await cls.engine.get_song_details(song_id)
                    if full_data:
                        song_data.update(full_data)

            media_url = await cls.engine.get_media_url(song_data)
            if not media_url:
                return None, None

            # Stream directly from CDN — no temp-file download needed.
            before_opts, opts = cls._build_ffmpeg_options(seek, effects, eq_preset, is_http=True)
            source = make_stream_source(media_url, before_opts, opts)
            title = cls.engine.format_string(song_data.get("song") or song_data.get("title") or "Unknown")
            return source, title
        except Exception as e:
            logger.error(f"Audio source error: {e}")
            return None, None

    @staticmethod
    def _has_media(song: dict) -> bool:
        """True when the song dict already carries enough to resolve a stream."""
        if song.get("encrypted_media_url") or song.get("media_url") or song.get("media_preview_url"):
            return True
        more_info = song.get("more_info") or {}
        if isinstance(more_info, dict):
            return bool(
                more_info.get("encrypted_media_url")
                or more_info.get("media_url")
                or more_info.get("media_preview_url")
            )
        return False

    @classmethod
    async def get_audio_source_for_song(cls, song: dict, volume: float = 1.0, effects: Optional[list[str]] = None, seek: Optional[float] = None, eq_preset: Optional[str] = None):
        try:
            song_data = dict(song)
            song_id = song_data.get("id")
            # Search results already carry full details (encrypted_media_url,
            # etc.). Only re-fetch when media info is missing, so queue
            # advancement / Next does not stall on an extra HTTP round trip.
            if song_id and not cls._has_media(song_data):
                full_data = await cls.engine.get_song_details(song_id)
                if full_data:
                    song_data.update(full_data)
            media_url = await cls.engine.get_media_url(song_data)
            if not media_url:
                return None, None

            # Stream directly from CDN — no temp-file download needed.
            before_opts, opts = cls._build_ffmpeg_options(seek, effects, eq_preset, is_http=True)
            source = make_stream_source(media_url, before_opts, opts)
            title = cls.engine.format_string(song_data.get("song") or song_data.get("title") or "Unknown")
            return source, title
        except Exception as e:
            logger.error(f"Audio source error: {e}")
            return None, None
