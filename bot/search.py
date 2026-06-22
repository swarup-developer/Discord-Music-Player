import logging
from typing import List, Dict, Any
from .youtube import YouTubeHandler
from .jiosaavn import JioSaavnHandler

logger = logging.getLogger(__name__)

async def search_songs(query: str, provider: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Unified search helper to query songs from YouTube or JioSaavn.
    """
    if provider == "youtube":
        return await YouTubeHandler.search(query, limit=limit)
    elif provider == "jiosaavn":
        results = await JioSaavnHandler.engine.search(query)
        if not results:
            return []
        return results[:limit]
    return []
