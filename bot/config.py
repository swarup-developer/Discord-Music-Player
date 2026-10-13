import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BOT_SYNC_GUILD_ID = os.getenv("BOT_SYNC_GUILD_ID")
INWORLD_API_KEY = os.getenv("INWORLD_API_KEY")
INWORLD_TTS_VOICE_ID = os.getenv("INWORLD_TTS_VOICE_ID", "")
INWORLD_STT_MODEL_ID = os.getenv("INWORLD_STT_MODEL_ID", "assemblyai/universal-streaming-multilingual")
# Optional HTTP proxy for all YouTube traffic (yt-dlp, FFmpeg, downloads), e.g.
# Cloudflare WARP in proxy mode: http://127.0.0.1:40000. Datacenter IPs get
# "Sign in to confirm you're not a bot"; routing through WARP avoids that
# without cookies. googlevideo URLs are bound to the resolving IP, so FFmpeg
# must use the same proxy as the resolver (ytjs: see its supervisor env).
YOUTUBE_PROXY = os.getenv("YOUTUBE_PROXY", "").strip() or None
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "AIzaSyD1c1Z_11-p5C4LdGPAYDyNreSJkfK6apE")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("music_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

# httpx logs every request at INFO; the IPTV health check alone makes
# thousands. The bot logs its own meaningful lines instead.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Enable verbose logging to debug voice issues.
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.voice_state").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
