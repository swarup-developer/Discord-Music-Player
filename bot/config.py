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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("music_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

# Enable verbose logging to debug voice issues.
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.voice_state").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
