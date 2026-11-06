"""/spotify commands: real Spotify playback from the user's own Premium account."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .spotify_connect import (
    IDLE_SECONDS,
    PCM_BEFORE_OPTIONS,
    REDIRECT_URI,
    PremiumRequired,
    SessionManager,
    SpotifyAuth,
    SpotifyError,
    UserStore,
    find_librespot,
    parse_spotify_link,
    valid_client_id,
)

logger = logging.getLogger(__name__)

SETUP_TEXT = f"""**Connect your Spotify Premium account** (one time, about 3 minutes)

1. Open https://developer.spotify.com/dashboard and log in with your **Premium** account.
2. Click **Create app**. Name and description can be anything.
3. Redirect URI: paste exactly `{REDIRECT_URI}` and click **Add**.
4. Under "Which API/SDKs are you planning to use", tick **Web API**, accept the terms and **Save**.
5. Open **Settings** and copy the **Client ID** (no Client Secret needed).
6. Press **Enter Client ID** below.

Spotify Premium is required. Note: this plays Spotify through an unofficial player, which Spotify's terms don't allow; use it at your own risk."""

AFTER_LOGIN_TEXT = """**Log in with Spotify**

1. Press **Log in with Spotify** and click **Agree**.
2. Your browser then shows a page that "can't be reached". That is expected.
3. Copy the **whole address** from the browser's address bar (it starts with `http://127.0.0.1:8888/callback?code=`).
4. Press **Paste the address** and paste it."""


class ClientIdModal(discord.ui.Modal, title="Your Spotify Client ID"):
    client_id = discord.ui.TextInput(label="Client ID (32 letters and numbers)", min_length=32, max_length=40)

    def __init__(self, cog: "SpotifyCog"):
        super().__init__(timeout=600)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        cid = self.client_id.value.strip()
        if not valid_client_id(cid):
            return await interaction.response.send_message(
                "That isn't a Client ID. It's 32 letters and numbers from your app's Settings page.", ephemeral=True
            )
        url = self.cog.auth.login_url(interaction.user.id, cid)
        view = discord.ui.View(timeout=900)
        view.add_item(discord.ui.Button(label="Log in with Spotify", url=url))
        view.add_item(PasteButton(self.cog))
        await interaction.response.send_message(AFTER_LOGIN_TEXT, view=view, ephemeral=True)


class PasteModal(discord.ui.Modal, title="Paste the address"):
    address = discord.ui.TextInput(
        label="The whole address from your browser",
        style=discord.TextStyle.paragraph, max_length=1500,
        placeholder="http://127.0.0.1:8888/callback?code=...",
    )

    def __init__(self, cog: "SpotifyCog"):
        super().__init__(timeout=900)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self.cog.auth.finish_login(interaction.user.id, self.address.value)
        except SpotifyError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        except Exception:
            logger.exception("Spotify login failed")
            return await interaction.followup.send("Spotify login failed. Try `/spotify setup` again.", ephemeral=True)
        await interaction.followup.send(
            f"Spotify connected as **{data['display_name']}**. Join a voice channel, use `/join`, then `/spotify play`.",
            ephemeral=True,
        )


class PasteButton(discord.ui.Button):
    def __init__(self, cog: "SpotifyCog"):
        super().__init__(label="Paste the address", style=discord.ButtonStyle.primary)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PasteModal(self.cog))


class SetupView(discord.ui.View):
    def __init__(self, cog: "SpotifyCog"):
        super().__init__(timeout=900)
        self.cog = cog

    @discord.ui.button(label="Enter Client ID", style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ClientIdModal(self.cog))


def _fmt_ms(ms: int) -> str:
    s = max(0, int(ms or 0)) // 1000
    return f"{s // 60}:{s % 60:02d}"


class SpotifyCog(commands.Cog):
    spotify = app_commands.Group(name="spotify", description="Play music from your own Spotify Premium account")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = UserStore()
        self.auth = SpotifyAuth(self.store)
        self.sessions = SessionManager(self.auth)
        self._sources: dict[int, discord.AudioSource] = {}
        self._idle_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        self._idle_task = asyncio.create_task(self._idle_loop())

    async def cog_unload(self):
        if self._idle_task:
            self._idle_task.cancel()
        await self.sessions.stop_all()

    # ------------------------------------------------------------ helpers --

    @property
    def music(self):
        return self.bot.get_cog("MusicCog")

    def _voice_client(self, guild_id: int) -> Optional[discord.VoiceClient]:
        music = self.music
        vc = music._session(guild_id).voice_client if music else None
        return vc if vc and vc.is_connected() else None

    async def _check_voice(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        if not interaction.guild:
            await interaction.followup.send("Use this in a server.", ephemeral=True)
            return None
        user_vc = interaction.user.voice.channel if interaction.user.voice else None
        vc = self._voice_client(interaction.guild.id)
        if not user_vc:
            await interaction.followup.send("Join a voice channel first.", ephemeral=True)
            return None
        if not vc:
            await interaction.followup.send("Use `/join` first so the bot is in your voice channel.", ephemeral=True)
            return None
        if vc.channel.id != user_vc.id:
            await interaction.followup.send("You need to be in the same voice channel as the bot.", ephemeral=True)
            return None
        return vc

    def _attach_audio(self, guild_id: int, vc: discord.VoiceClient, session):
        current = self._sources.get(guild_id)
        if current is not None and vc.source is current and (vc.is_playing() or vc.is_paused()):
            if vc.is_paused():
                vc.resume()
            return
        music = self.music
        if music:
            # Stop whatever /play was doing without advancing its queue.
            music._stop_current_track_for_manual_replace(guild_id)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        volume = music.state.get_volume(guild_id) if music else 0.5
        pcm = discord.FFmpegPCMAudio(str(session.fifo), before_options=PCM_BEFORE_OPTIONS, options="-vn")
        source = discord.PCMVolumeTransformer(pcm, volume=volume ** 2)
        self._sources[guild_id] = source

        def after(error, gid=guild_id, src=source):
            if error:
                logger.warning("Spotify audio ended with error: %s", error)
            if self._sources.get(gid) is src:
                self._sources.pop(gid, None)
                s = self.sessions.get(gid)
                if s:
                    asyncio.run_coroutine_threadsafe(self._quiet_pause(s), self.bot.loop)

        vc.play(source, after=after)

    async def _quiet_pause(self, session):
        with contextlib.suppress(Exception):
            await session.call("/player/pause")

    def _on_event(self, session, etype: str, data: dict):
        """Follow pause/resume done from the Spotify app on someone's phone."""
        vc = self._voice_client(session.guild_id)
        src = self._sources.get(session.guild_id)
        if not vc or src is None or vc.source is not src:
            return
        if etype in ("paused", "stopped", "not_playing") and vc.is_playing():
            vc.pause()
        elif etype == "playing" and vc.is_paused():
            vc.resume()

    async def _idle_loop(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for gid, s in list(self.sessions.by_guild.items()):
                vc = self._voice_client(gid)
                ours = vc is not None and self._sources.get(gid) is not None and vc.source is self._sources.get(gid)
                if not s.alive:
                    await self.sessions.stop(gid)
                elif not vc and now - s.last_activity > 60:
                    await self.sessions.stop(gid)
                elif (s.paused or not ours) and now - s.last_activity > IDLE_SECONDS:
                    await self.sessions.stop(gid)

    async def _session_or_reply(self, interaction: discord.Interaction):
        s = self.sessions.get(interaction.guild.id) if interaction.guild else None
        if not s:
            await interaction.followup.send("Spotify isn't playing here. Start it with `/spotify play`.", ephemeral=True)
        return s

    async def _send_error(self, interaction: discord.Interaction, e: Exception):
        msg = str(e) if isinstance(e, SpotifyError) else "Something went wrong with Spotify. Try again."
        if not isinstance(e, SpotifyError):
            logger.exception("Spotify command failed", exc_info=e)
        with contextlib.suppress(Exception):
            await interaction.followup.send(msg, ephemeral=True)

    # ----------------------------------------------------------- commands --

    @spotify.command(name="setup", description="Connect your Spotify Premium account (one time)")
    async def setup_cmd(self, interaction: discord.Interaction):
        if not find_librespot():
            return await interaction.response.send_message(
                "Spotify playback isn't installed on this bot's server yet. Ask the bot owner.", ephemeral=True
            )
        await interaction.response.send_message(SETUP_TEXT, view=SetupView(self), ephemeral=True,
                                                suppress_embeds=True)

    @spotify.command(name="play", description="Play a song, album or playlist from Spotify (name or link)")
    @app_commands.describe(query="Song name, or a Spotify track / album / playlist link")
    async def play_cmd(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)
        vc = await self._check_voice(interaction)
        if not vc:
            return
        try:
            uri = parse_spotify_link(query)
            info = None
            if uri and uri.startswith("spotify:track:"):
                info = await self.auth.track(interaction.user.id, uri.split(":")[-1])
            elif not uri:
                results = await self.auth.search(interaction.user.id, query, limit=1)
                if not results:
                    return await interaction.followup.send(f"Nothing found on Spotify for **{query}**.")
                info = results[0]
                uri = info["uri"]
            session = await self.sessions.ensure(interaction.guild.id, interaction.user.id)
            session.on_event = self._on_event
            self._attach_audio(interaction.guild.id, vc, session)
            await session.play(uri)
        except Exception as e:
            return await self._send_error(interaction, e)

        if info:
            embed = discord.Embed(title=info["name"], description=f"{info['artists']}\n{info['album']}",
                                  color=discord.Color.green())
            if info.get("image"):
                embed.set_thumbnail(url=info["image"])
            embed.set_footer(text=f"Spotify, {_fmt_ms(info['duration_ms'])}, requested by {interaction.user.display_name}")
            await interaction.followup.send(content="Now playing on Spotify:", embed=embed)
        else:
            kind = uri.split(":")[1]
            await interaction.followup.send(f"Playing this Spotify {kind}. Use `/spotify now` to see the song.")

    @spotify.command(name="queue", description="Add a Spotify song to play next")
    @app_commands.describe(query="Song name or Spotify track link")
    async def queue_cmd(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)
        if not await self._check_voice(interaction):
            return
        s = await self._session_or_reply(interaction)
        if not s:
            return
        try:
            uri = parse_spotify_link(query)
            if uri and not uri.startswith("spotify:track:"):
                return await interaction.followup.send("Only single songs can be queued. Use `/spotify play` for albums and playlists.")
            if uri:
                info = await self.auth.track(s.user_id, uri.split(":")[-1])
            else:
                results = await self.auth.search(s.user_id, query, limit=1)
                if not results:
                    return await interaction.followup.send(f"Nothing found on Spotify for **{query}**.")
                info = results[0]
            await s.call("/player/add_to_queue", {"uri": info["uri"]})
        except Exception as e:
            return await self._send_error(interaction, e)
        await interaction.followup.send(f"Queued on Spotify: **{info['name']}** by {info['artists']}")

    async def _simple(self, interaction: discord.Interaction, path: str, done: str, body: Optional[dict] = None):
        await interaction.response.defer(thinking=True)
        if not await self._check_voice(interaction):
            return
        s = await self._session_or_reply(interaction)
        if not s:
            return
        try:
            await s.call(path, body)
        except Exception as e:
            return await self._send_error(interaction, e)
        vc = self._voice_client(interaction.guild.id)
        if vc and self._sources.get(interaction.guild.id) is vc.source:
            if path == "/player/pause" and vc.is_playing():
                vc.pause()
            elif path in ("/player/resume", "/player/next", "/player/prev") and vc.is_paused():
                vc.resume()
        await interaction.followup.send(done)

    @spotify.command(name="pause", description="Pause Spotify")
    async def pause_cmd(self, interaction: discord.Interaction):
        await self._simple(interaction, "/player/pause", "Paused.")

    @spotify.command(name="resume", description="Resume Spotify")
    async def resume_cmd(self, interaction: discord.Interaction):
        await self._simple(interaction, "/player/resume", "Resumed.")

    @spotify.command(name="skip", description="Skip to the next Spotify song")
    async def skip_cmd(self, interaction: discord.Interaction):
        await self._simple(interaction, "/player/next", "Skipped.")

    @spotify.command(name="previous", description="Go back to the previous Spotify song")
    async def previous_cmd(self, interaction: discord.Interaction):
        await self._simple(interaction, "/player/prev", "Going back.")

    @spotify.command(name="volume", description="Set the bot's volume (1 to 100)")
    @app_commands.describe(level="1 to 100")
    async def volume_cmd(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 100]):
        await interaction.response.defer(thinking=True, ephemeral=True)
        if not await self._check_voice(interaction):
            return
        if self.music:
            self.music.sync_voice_volume(interaction.guild.id, level / 100)
        await interaction.followup.send(f"Volume set to {level}.", ephemeral=True)

    @spotify.command(name="now", description="Show the Spotify song that's playing")
    async def now_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        s = await self._session_or_reply(interaction)
        if not s:
            return
        m = s.now_playing
        if not m:
            return await interaction.followup.send("Nothing is playing on Spotify right now.")
        artists = m.get("artist_names") or []
        embed = discord.Embed(
            title=m.get("name", "Unknown"),
            description=f"{', '.join(artists) if isinstance(artists, list) else artists}\n{m.get('album_name', '')}",
            color=discord.Color.green(),
        )
        if m.get("album_cover_url"):
            embed.set_thumbnail(url=m["album_cover_url"])
        state = "Paused" if s.paused else "Playing"
        embed.set_footer(text=f"{state} on Spotify, {_fmt_ms(m.get('duration', 0))}")
        await interaction.followup.send(embed=embed)

    @spotify.command(name="stop", description="Stop Spotify and free its memory on the server")
    async def stop_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        if not interaction.guild:
            return
        src = self._sources.pop(interaction.guild.id, None)
        vc = self._voice_client(interaction.guild.id)
        if vc and src is not None and vc.source is src:
            vc.stop()
        await self.sessions.stop(interaction.guild.id)
        await interaction.followup.send("Spotify stopped.")

    @spotify.command(name="logout", description="Remove your Spotify login from this bot")
    async def logout_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.sessions.stop_user(interaction.user.id)
        removed = self.auth.forget(interaction.user.id)
        await interaction.followup.send(
            "Your Spotify login was deleted from this bot." if removed else "You weren't logged in.",
            ephemeral=True,
        )
