import asyncio
import logging

import discord
import httpx
from discord.ext import commands

from .state import BotState

logger = logging.getLogger(__name__)


class ControlCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state: BotState = bot.music_state

    @discord.app_commands.command(name="volume", description="Set playback volume for this server")
    async def volume(self, interaction: discord.Interaction, level: int):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        if self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.response.send_message(
                "This bot is already active in another server. Leave that server's voice channel first.",
                ephemeral=True,
            )
        if not (1 <= level <= 200):
            return await interaction.response.send_message("Volume must be between 1 and 200.", ephemeral=True)
        volume = level / 100
        self.state.set_volume(interaction.guild.id, volume)
        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog and hasattr(music_cog, "sync_voice_volume"):
            music_cog.sync_voice_volume(interaction.guild.id, volume)
        await interaction.response.send_message(f"Volume set to {level}%", ephemeral=True)

    @discord.app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction):
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.response.send_message(
                "This bot is already active in another server. Leave that server's voice channel first.",
                ephemeral=True,
            )
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        session = self.state.session_for(interaction.guild.id)
        if session.voice_client and session.voice_client.is_playing():
            session.advance_queue_on_stop = True
            session.voice_client.stop()
            await interaction.response.send_message("Skipped to the next track.")
        else:
            await interaction.response.send_message("Nothing is playing.")

    @discord.app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.response.send_message(
                "This bot is already active in another server. Leave that server's voice channel first.",
                ephemeral=True,
            )
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        session = self.state.session_for(interaction.guild.id)
        if session.voice_client and session.voice_client.is_playing():
            session.voice_client.pause()
            import time
            session.last_paused_at = time.time()
            await interaction.response.send_message("Playback paused.")
        else:
            await interaction.response.send_message("Nothing is playing.")

    @discord.app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.response.send_message(
                "This bot is already active in another server. Leave that server's voice channel first.",
                ephemeral=True,
            )
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        session = self.state.session_for(interaction.guild.id)
        if session.voice_client and session.voice_client.is_paused():
            session.voice_client.resume()
            import time
            if getattr(session, "last_paused_at", None):
                session.paused_duration = getattr(session, "paused_duration", 0.0) + (time.time() - session.last_paused_at)
                session.last_paused_at = None
            await interaction.response.send_message("Playback resumed.")
        else:
            await interaction.response.send_message("Nothing is paused.")

    @discord.app_commands.command(name="stop", description="Stop playback")
    async def stop(self, interaction: discord.Interaction):
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.response.send_message(
                "This bot is already active in another server. Leave that server's voice channel first.",
                ephemeral=True,
            )
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        session = self.state.session_for(interaction.guild.id)
        if session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused()):
            session.advance_queue_on_stop = False
            session.voice_client.stop()
            self.state.clear_queue(interaction.guild.id)
            session.start_time = 0.0
            session.paused_duration = 0.0
            session.last_paused_at = None
            await interaction.response.send_message("Stopped.")
        else:
            await interaction.response.send_message("Nothing is playing.")

    @discord.app_commands.command(name="go", description="Leave voice channel")
    async def go(self, interaction: discord.Interaction):
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.response.send_message(
                "This bot is already active in another server. Leave that server's voice channel first.",
                ephemeral=True,
            )
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        session = self.state.session_for(interaction.guild.id)
        if session.voice_client:
            await session.voice_client.disconnect()
            self.state.clear(interaction.guild.id)
            self.state.clear_queue(interaction.guild.id)
            await interaction.response.send_message("Goodbye!")
        else:
            await interaction.response.send_message("I'm not in a voice channel.")

    @discord.app_commands.command(name="diagnose", description="Diagnose voice connection issues")
    async def diagnose(self, interaction: discord.Interaction):
        import socket
        await interaction.response.defer(ephemeral=True)
        results = []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get("https://discord.com/api/v10/gateway")
            results.append("OK: Discord REST API reachable" if r.status_code == 200 else f"WARN: Discord REST API returned HTTP {r.status_code}")
        except Exception as e:
            results.append(f"ERROR: Discord REST API unreachable: {e}")
        for host in ["discord.media", "voice.discord.media", "router.discord.gg"]:
            try:
                ip = await asyncio.get_event_loop().run_in_executor(None, socket.gethostbyname, host)
                results.append(f"OK: DNS resolved `{host}` -> `{ip}`")
            except Exception as e:
                results.append(f"ERROR: DNS failed for `{host}`: {e}")
        guild_vc = interaction.guild.voice_client if interaction.guild else None
        results.append(f"OK: Bot voice client is currently connected to `{guild_vc.channel}`" if guild_vc and guild_vc.is_connected() else "INFO: Bot is not currently in any voice channel")
        results.append(f"{'OK' if discord.opus.is_loaded() else 'ERROR'} Opus library: {'loaded' if discord.opus.is_loaded() else 'NOT loaded'}")
        embed = discord.Embed(title="Voice Diagnostics", description="\n".join(results), color=discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)

