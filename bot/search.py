import logging
from typing import List, Dict, Any
from .youtube import YouTubeHandler
from .jiosaavn import JioSaavnHandler
from .soundcloud import SoundCloudHandler

logger = logging.getLogger(__name__)

async def search_songs(query: str, provider: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Unified search helper to query songs from YouTube, JioSaavn, or SoundCloud.
    """
    if provider == "youtube":
        return await YouTubeHandler.search(query, limit=limit)
    elif provider == "jiosaavn":
        results = await JioSaavnHandler.engine.search(query)
        if not results:
            return []
        return results[:limit]
    elif provider == "soundcloud":
        return await SoundCloudHandler.search(query, limit=limit)
    elif provider == "local":
        from .local_files import LocalFileHandler
        files = LocalFileHandler.list_files()
        query_lower = query.lower()
        results = []
        for f in files:
            if query_lower in f.lower():
                abs_path = LocalFileHandler.get_absolute_path(f)
                if abs_path:
                    results.append({
                        "title": f"📁 Local: {f}",
                        "song": f,
                        "album": "Local Files",
                        "url": abs_path,
                        "id": abs_path,
                        "provider": "local"
                    })
        return results[:limit]
    elif provider == "spotify":
        # Fallback to YouTube search for Spotify provider queries
        return await YouTubeHandler.search(query, limit=limit)
    return []
