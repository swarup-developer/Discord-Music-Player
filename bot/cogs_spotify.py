"""Spotify (Premium) through Spotify Soloist, Spotify's official player.

Users only run /spotify login once (paste their Soloist key). Playback uses
the bot's normal commands: choose Spotify in /provider, then /play; pause,
resume, skip, previous, seek, stop and volume control it while it plays.
MusicCog calls play() and control() here.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .soloist_player import (
    IDLE_SECONDS,
    KEY_PAGE,
    PULSE_SERVER,
    SoloistManager,
    ffmpeg_input,
    find_soloist,
    valid_key,
)
from .spotify_connect import SpotifyError, UserStore, parse_spotify_link

logger = logging.getLogger(__name__)

# FFmpeg (started by discord.py) records from the bot's private PulseAudio.
os.environ.setdefault("PULSE_SERVER", PULSE_SERVER)

LOGIN_TEXT = (
    "**Connect Spotify (Premium)**\n"
    "1. Press **Get your key** and log in with your Spotify Premium account. "
    "Create a key there and copy it (it starts with `spak_`).\n"
    "2. Press **Paste key** and paste it.\n\n"
    "That's all. Your key is stored encrypted and removed with `/spotify logout`."
)


class KeyModal(discord.ui.Modal, title="Paste your Spotify key"):
    key = discord.ui.TextInput(label="Key (starts with spak_)", min_length=20, max_length=200,
                               placeholder="spak_...")

    def __init__(self, cog: "SpotifyCog"):
        super().__init__(timeout=900)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key.value.strip()
        if not valid_key(key):
            return await interaction.response.send_message(
                "That doesn't look like a Spotify key. It starts with `spak_`. Copy it again from the key page.",
                ephemeral=True,
            )
        await interaction.response.send_message("Connecting to Spotify, one moment.", ephemeral=True)

        async def on_step(text: str, url: str):
            view = discord.ui.View(timeout=600)
            view.add_item(discord.ui.Button(label="Approve on Spotify", url=url))
            await interaction.followup.send(
                "Spotify wants you to approve this player once. Press **Approve on Spotify**, "
                "log in if asked, and confirm. I'll continue by myself.",
                view=view, ephemeral=True,
            )

        try:
            await self.cog.players.login(interaction.user.id, key, on_step)
        except SpotifyError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        except Exception:
            logger.exception("Spotify login failed")
            return await interaction.followup.send("Spotify login failed. Try `/spotify login` again.", ephemeral=True)
        await interaction.followup.send(
            "Spotify is connected. Choose **Spotify** in `/provider`, then use `/play` as usual.", ephemeral=True
        )


class LoginView(discord.ui.View):
    def __init__(self, cog: "SpotifyCog"):
        super().__init__(timeout=900)
        self.cog = cog
        self.add_item(discord.ui.Button(label="Get your key", url=KEY_PAGE))

    @discord.ui.button(label="Paste key", style=discord.ButtonStyle.primary)
    async def paste(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyModal(self.cog))


class SpotifyCog(commands.Cog):
    spotify = app_commands.Group(name="spotify", description="Connect your Spotify Premium account")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = UserStore()
        self.players = SoloistManager(self.store)
        self._sources: dict[int, discord.AudioSource] = {}
        self._idle_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        self._idle_task = asyncio.create_task(self._idle_loop())

    async def cog_unload(self):
        if self._idle_task:
            self._idle_task.cancel()
        await self.players.stop_all()

    # ---------------------------------------------------------- helpers ---

    @property
    def music(self):
        return self.bot.get_cog("MusicCog")

    def logged_in(self, user_id: int) -> bool:
        return self.players.logged_in(user_id)

    def _voice_client(self, guild_id: int) -> Optional[discord.VoiceClient]:
        music = self.music
        vc = music._session(guild_id).voice_client if music else None
        return vc if vc and vc.is_connected() else None

    def is_active(self, guild_id: int) -> bool:
        vc = self._voice_client(guild_id)
        src = self._sources.get(guild_id)
        return bool(vc and src is not None and vc.source is src and (vc.is_playing() or vc.is_paused()))

    def _attach_audio(self, guild_id: int, vc: discord.VoiceClient, session):
        if self.is_active(guild_id):
            if vc.is_paused():
                vc.resume()
            return
        music = self.music
        if music:
            music._stop_current_track_for_manual_replace(guild_id)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        volume = music.state.get_volume(guild_id) if music else 0.5
        source_in, before = ffmpeg_input(session.sink)
        pcm = discord.FFmpegPCMAudio(source_in, before_options=before, options="-vn")
        source = discord.PCMVolumeTransformer(pcm, volume=volume ** 2)
        self._sources[guild_id] = source

        def after(error, gid=guild_id, src=source):
            if error:
                logger.warning("Spotify audio ended with error: %s", error)
            if self._sources.get(gid) is src:
                self._sources.pop(gid, None)
                self._clear_now_playing(gid)
                asyncio.run_coroutine_threadsafe(self.players.stop(gid), self.bot.loop)

        vc.play(source, after=after)

    def _clear_now_playing(self, guild_id: int):
        music = self.music
        if not music:
            return
        s = music._session(guild_id)
        if getattr(s, "spotify_song", None) is not None:
            s.spotify_song = None
            s.current_song_title = None
            s.is_playing = False

    def _on_event(self, session, etype: str, data: dict):
        guild_id = session.guild_id
        music = self.music
        np = session.now_playing
        if music and np:
            s = music._session(guild_id)
            title = f"{np['name']} - {np['artists']}" if np.get("artists") else np["name"]
            if s.current_song_title != title:
                s.current_song_title = title
                s.current_song_data = None
                s.spotify_song = {"duration": (int(np.get("duration_ms") or 0) / 1000) or None,
                                  "image": np.get("image")}
                s.is_playing = True
                s.start_time = time.time() - session.position_ms / 1000
                s.paused_duration = 0.0
                s.last_paused_at = None
                asyncio.create_task(music.refresh_controller(guild_id))
        vc = self._voice_client(guild_id)
        if vc and self.is_active(guild_id):
            if session.paused and vc.is_playing():
                vc.pause()
            elif not session.paused and vc.is_paused():
                vc.resume()

    async def _idle_loop(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for gid, s in list(self.players.by_guild.items()):
                if not s.alive:
                    self._sources.pop(gid, None)
                    await self.players.stop(gid)
                elif not self._voice_client(gid) and now - s.last_activity > 60:
                    await self.players.stop(gid)
                elif (s.paused or not self.is_active(gid)) and now - s.last_activity > IDLE_SECONDS:
                    src = self._sources.pop(gid, None)
                    vc = self._voice_client(gid)
                    if vc and src is not None and vc.source is src:
                        vc.stop()
                    await self.players.stop(gid)

    # ------------------------------------------------ called by MusicCog ---

    async def play(self, interaction: discord.Interaction, query: str):
        guild_id = interaction.guild.id
        vc = self._voice_client(guild_id)
        if not vc:
            return await interaction.followup.send("Use `/join` first so the bot is in your voice channel.")
        current = self.players.get(guild_id)
        account = current.user_id if current else interaction.user.id
        if not current and not self.logged_in(account):
            return await interaction.followup.send(
                "This server plays from **Spotify**. Connect your Spotify Premium account once with "
                "`/spotify login`, or pick another source with `/provider`."
            )
        try:
            uri = parse_spotify_link(query)
            info = None
            if uri and uri.startswith("spotify:track:"):
                info = await self.players.search.track_by_id(uri.split(":")[-1])
            elif not uri:
                info = await self.players.search.track(query)
                if not info:
                    return await interaction.followup.send(f"Nothing found on Spotify for **{query}**.")
                uri = info["uri"]

            if current and self.is_active(guild_id) and not current.paused and uri.startswith("spotify:track:"):
                await current.send("add_to_queue", uri=uri)
                name = f"**{info['name']}** - {info['artists']}" if info else "the song"
                return await interaction.followup.send(f"Queued {name}.")

            session = await self.players.ensure(guild_id, account)
            session.on_event = self._on_event
            self._attach_audio(guild_id, vc, session)
            await session.play(uri)
        except SpotifyError as e:
            return await interaction.followup.send(str(e))
        except Exception:
            logger.exception("Spotify play failed")
            return await interaction.followup.send("Spotify couldn't play that. Try again.")

        if info:
            await interaction.followup.send(f"Playing from Spotify: **{info['name']}** - {info['artists']}")
        else:
            await interaction.followup.send(f"Playing this Spotify {uri.split(':')[1]}.")

    async def control(self, guild_id: int, action: str, seconds: float = 0) -> str:
        s = self.players.get(guild_id)
        vc = self._voice_client(guild_id)
        if not s:
            return "Spotify isn't playing."
        try:
            if action == "pause":
                await s.send("pause")
                s.paused = True
                if vc and vc.is_playing():
                    vc.pause()
                return "Paused."
            if action == "resume":
                await s.send("play")
                s.paused = False
                if vc and vc.is_paused():
                    vc.resume()
                return "Resumed."
            if action == "next":
                await s.send("skip_next")
                if vc and vc.is_paused():
                    vc.resume()
                return "Skipped to the next song."
            if action == "prev":
                await s.send("skip_prev")
                if vc and vc.is_paused():
                    vc.resume()
                return "Playing the previous song."
            if action == "seek":
                pos = max(0, s.current_position() + int(seconds * 1000))
                await s.send("seek", position_ms=pos)
                s.position_ms, s.position_at = pos, time.time()
                return f"Moved {'forward' if seconds > 0 else 'back'} {abs(int(seconds))} seconds."
            if action == "stop":
                src = self._sources.pop(guild_id, None)
                self._clear_now_playing(guild_id)
                if vc and src is not None and vc.source is src:
                    vc.stop()
                await self.players.stop(guild_id)
                return "Stopped."
        except SpotifyError as e:
            return str(e)
        return "Unknown action."

    # ----------------------------------------------------------- commands ---

    @spotify.command(name="login", description="Connect your Spotify Premium account (one time)")
    async def login_cmd(self, interaction: discord.Interaction):
        if not find_soloist():
            return await interaction.response.send_message(
                "Spotify playback isn't installed on this bot's server yet. Ask the bot owner.", ephemeral=True
            )
        if self.logged_in(interaction.user.id):
            return await interaction.response.send_message(
                "You're already connected. Choose **Spotify** in `/provider` and use `/play`. "
                "To use a different account, run `/spotify logout` first.",
                ephemeral=True,
            )
        await interaction.response.send_message(LOGIN_TEXT, view=LoginView(self), ephemeral=True)

    @spotify.command(name="logout", description="Remove your Spotify login from this bot")
    async def logout_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        for gid, s in list(self.players.by_guild.items()):
            if s.user_id == interaction.user.id:
                await self.control(gid, "stop")
        removed = self.players.forget(interaction.user.id)
        await interaction.followup.send(
            "Your Spotify login was deleted from this bot." if removed else "You weren't logged in.",
            ephemeral=True,
        )
