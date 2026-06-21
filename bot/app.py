import logging
import gc
import ctypes

import discord
from discord.ext import commands, tasks

from .config import BOT_SYNC_GUILD_ID, INWORLD_API_KEY, INWORLD_STT_MODEL_ID, INWORLD_TTS_VOICE_ID
from .cogs_control import ControlCog
from .cogs_help import HelpCog
from .cogs_music import MusicCog
from .opus_bootstrap import ensure_opus_loaded
from .state import BotState

logger = logging.getLogger(__name__)


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents, max_messages=None)
        self.music_state = BotState()

    async def setup_hook(self):
        if not ensure_opus_loaded():
            logger.warning("Opus library not loaded. Voice mode will not work until Opus is installed.")
        await self.add_cog(HelpCog(self))
        await self.add_cog(ControlCog(self))
        await self.add_cog(MusicCog(self))

        # Start periodic memory optimization task
        self._periodic_gc.start()

        if BOT_SYNC_GUILD_ID:
            guild = discord.Object(id=int(BOT_SYNC_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        logger.info(f"Bot logged in as {self.user} (version: {discord.__version__})")
        if not discord.opus.is_loaded():
            logger.error("Opus library not loaded. Voice mode is disabled until Opus is installed.")

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        logger.exception("Unhandled app command error", exc_info=error)

        message = "Something went wrong while handling that command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass

    @tasks.loop(minutes=10)
    async def _periodic_gc(self):
        """Periodically run Python's GC and force release memory to Linux OS."""
        gc.collect()
        try:
            # Force release unmapped memory pages on Linux glibc
            libc = ctypes.CDLL("libc.so.6")
            libc.malloc_trim(0)
        except Exception:
            pass


def build_bot() -> Bot:
    return Bot()
