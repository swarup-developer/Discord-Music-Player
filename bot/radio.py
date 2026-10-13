"""Internet radio directory backed by Radio Browser (https://www.radio-browser.info).

Radio Browser is a free, community-maintained directory of public radio streams.
It checks every stream daily and flags broken ones, so lists built from it stay
playable -- unlike a hard-coded preset file, whose URLs rot within months.
"""
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Radio Browser asks clients to send a descriptive User-Agent and to spread load
# across its mirrors; any mirror serves the same data.
_HOSTS = (
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
)
_HEADERS = {"User-Agent": "Discord-Music-Player/1.0 (+https://github.com/swarup-developer/Discord-Music-Player)"}
_CACHE_TTL = 30 * 60
_cache: Dict[str, tuple] = {}


def _station(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = raw.get("url_resolved") or raw.get("url")
    name = (raw.get("name") or "").strip()
    if not url or not name or not url.startswith(("http://", "https://")):
        return None
    tags = [t for t in (raw.get("tags") or "").split(",") if t][:3]
    return {
        "uuid": raw.get("stationuuid"),
        "name": name[:90],
        "url": url,
        "country": raw.get("countrycode") or "",
        "language": (raw.get("language") or "").split(",")[0].title(),
        "tags": ", ".join(tags),
        "codec": raw.get("codec") or "",
        "bitrate": raw.get("bitrate") or 0,
    }


async def _query(path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    key = path + repr(sorted(params.items()))
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    params = {"hidebroken": "true", **params}
    async with httpx.AsyncClient(headers=_HEADERS, timeout=8.0, follow_redirects=True) as client:
        for host in _HOSTS:
            try:
                resp = await client.get(host + path, params=params)
                if resp.status_code != 200:
                    continue
                seen, stations = set(), []
                for raw in resp.json():
                    st = _station(raw)
                    # The directory often lists one station several times
                    # (mirrors, bitrates); keep the most popular entry.
                    if st and st["name"].lower() not in seen:
                        seen.add(st["name"].lower())
                        stations.append(st)
                _cache[key] = (time.time() + _CACHE_TTL, stations)
                return stations
            except Exception as e:
                logger.warning("Radio Browser %s failed: %s", host, e)
    return []


async def top_stations(country_code: str = "IN", limit: int = 25) -> List[Dict[str, Any]]:
    """Most-played working stations for a country."""
    return (await _query("/json/stations/search", {
        "countrycode": country_code.upper(),
        "order": "clickcount",
        "reverse": "true",
        "limit": limit * 2,
    }))[:limit]


async def search_stations(query: str, country_code: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
    """Search working stations by name, falling back to a tag (genre) search."""
    params = {"order": "clickcount", "reverse": "true", "limit": limit * 2}
    if country_code:
        params["countrycode"] = country_code.upper()
    results = await _query("/json/stations/search", {**params, "name": query})
    if not results:
        results = await _query("/json/stations/search", {**params, "tag": query.lower()})
    return results[:limit]


async def register_click(uuid: Optional[str]) -> None:
    """Tell Radio Browser a station was played (keeps its popularity ranking honest)."""
    if not uuid:
        return
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=4.0) as client:
            await client.get(f"{_HOSTS[0]}/json/url/{uuid}")
    except Exception:
        pass
