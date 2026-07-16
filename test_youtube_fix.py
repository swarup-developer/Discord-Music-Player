import asyncio
import logging
import time
from bot.youtube import YouTubeHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

async def run_tests():
    # 1. Test Search
    logger.info("Starting YouTube Search test...")
    start = time.time()
    try:
        results = await YouTubeHandler.search("never gonna give you up", limit=2)
        logger.info(f"Search results received in {time.time()-start:.2f}s:")
        for res in results:
            logger.info(f" - [{res['id']}] {res['title']}")
    except Exception as e:
        logger.error(f"Search failed: {e}")

    # 2. Test Stream Extraction
    logger.info("\nStarting YouTube Stream Extraction test...")
    video_url = "https://www.youtube.com/watch?v=JGwWNGJdvx8"
    start = time.time()
    try:
        res = await YouTubeHandler.extract_stream_url_async(video_url)
        logger.info(f"Extraction result in {time.time()-start:.2f}s:")
        if res:
            stream_url, title, is_live, duration = res
            logger.info(f" - Title: {title}")
            logger.info(f" - Stream URL: {stream_url[:100]}...")
            logger.info(f" - Live: {is_live}, Duration: {duration}")
        else:
            logger.info(" - Failed to extract stream URL")
    except Exception as e:
        logger.error(f"Extraction failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_tests())
