import json
import logging
import re
from typing import Optional, List, Dict, Any
import urllib.request
import urllib.parse
import asyncio

logger = logging.getLogger(__name__)

SPOTIFY_URL_RE = re.compile(
    r'https?://open\.spotify\.com/(track|album|playlist)/([a-zA-Z0-9]+)'
)

class SpotifyHandler:
    @classmethod
    def is_spotify_url(cls, query: str) -> bool:
        return bool(SPOTIFY_URL_RE.search(query))

    @classmethod
    async def resolve_url(cls, url: str) -> Optional[Dict[str, Any]]:
        """
        Resolves a Spotify URL (track, album, or playlist) to its metadata.
        Returns:
            Dict containing:
                "type": "track" | "album" | "playlist"
                "name": Name of the item
                "tracks": List of Dicts, each with {"title": ..., "artist": ..., "duration_ms": ...}
        """
        match = SPOTIFY_URL_RE.search(url)
        if not match:
            return None

        resource_type = match.group(1)
        resource_id = match.group(2)
        
        embed_url = f"https://open.spotify.com/embed/{resource_type}/{resource_id}"
        
        loop = asyncio.get_event_loop()
        def _fetch_metadata():
            try:
                req = urllib.request.Request(
                    embed_url,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                )
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    html = resp.read().decode('utf-8')
                    
                # Search for __NEXT_DATA__
                json_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
                if not json_match:
                    logger.warning("Could not find __NEXT_DATA__ in Spotify embed HTML")
                    return None
                    
                data = json.loads(json_match.group(1))
                state_data = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {})
                entity = state_data.get("entity")
                if not entity:
                    logger.warning("Could not find entity in Spotify state data")
                    return None
                    
                result = {
                    "type": entity.get("type"),
                    "name": entity.get("name") or entity.get("title") or "Unknown",
                    "tracks": []
                }
                
                if result["type"] == "track":
                    artists = entity.get("artists", [])
                    artist_str = ", ".join([a.get("name") for a in artists]) if artists else "Unknown Artist"
                    result["tracks"].append({
                        "title": entity.get("name") or entity.get("title"),
                        "artist": artist_str,
                        "duration_ms": entity.get("duration", 0)
                    })
                elif result["type"] in ("album", "playlist"):
                    track_list = entity.get("trackList", [])
                    for t in track_list:
                        result["tracks"].append({
                            "title": t.get("title", "Unknown Track"),
                            "artist": t.get("subtitle", "Unknown Artist").replace("\xa0", " "),
                            "duration_ms": t.get("duration", 0)
                        })
                        
                return result
            except Exception as e:
                logger.error(f"Error fetching Spotify metadata: {e}")
                return None
                
        return await loop.run_in_executor(None, _fetch_metadata)
