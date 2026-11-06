"""Spotify playback from the user's own Premium account.

Only two commands of its own: /spotify login and /spotify logout. Playback
uses the bot's normal commands: pick Spotify in /provider, then /play searches
Spotify and plays from it, and pause / resume / skip / previous / seek / stop /
volume (commands and controller buttons) control Spotify while it's playing.
MusicCog calls into this cog through play() and control().
"""
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
    SessionManager,
    SpotifyAuth,
    SpotifyError,
    UserStore,
    find_librespot,
    parse_spotify_link,
    valid_client_id,
)

logger = logging.getLogger(__name__)

LOGIN_TEXT = f"""**Connect your Spotify Premium account** (one time, about 3 minutes)

1. Open https://developer.spotify.com/dashboard and log in with your **Premium** account.
2. Click **Create app**. Name and description can be anything.
3. Redirect URI: paste exactly `{REDIRECT_URI}` and click **Add**.
4. Tick **Web API**, accept the terms and **Save**.
5. Open **Settings** and copy the **Client ID** (no Client Secret needed).
6. Press **Enter Client ID** below.

Spotify Premium is required. This plays Spotify through an unofficial player, which Spotify's terms don't allow; use it at your own risk."""

AFTER_ID_TEXT = """**Log in with Spotify**

1. Press **Log in with Spotify** and click **Agree**.
2. Your browser then shows a page that "can't be reached". That is expected.
3. Copy the **whole address** from the address bar (it starts with `http://127.0.0.1:8888/callback?code=`).
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
        await interaction.response.send_message(AFTER_ID_TEXT, view=view, ephemeral=True)


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
            return await interaction.followup.send("Spotify login failed. Try `/spotify login` again.", ephemeral=True)
        await interaction.followup.send(
            f"Step 1 done: your Spotify app is connected as **{data['display_name']}**.", ephemeral=True
        )
        await self.cog.link_player(interaction)


class PasteButton(discord.ui.Button):
    def __init__(self, cog: "SpotifyCog"):
        super().__init__(label="Paste the address", style=discord.ButtonStyle.primary)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PasteModal(self.cog))


class LoginView(discord.ui.View):
    def __init__(self, cog: "SpotifyCog"):
        super().__init__(timeout=900)
        self.cog = cog

    @discord.ui.button(label="Enter Client ID", style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ClientIdModal(self.cog))


class SpotifyCog(commands.Cog):
    spotify = app_commands.Group(name="spotify", description="Connect your Spotify Premium account")

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

    # ---------------------------------------------------------- helpers ---

    @property
    def music(self):
        return self.bot.get_cog("MusicCog")

    def logged_in(self, user_id: int) -> bool:
        return self.store.get(user_id) is not None

    def _voice_client(self, guild_id: int) -> Optional[discord.VoiceClient]:
        music = self.music
        vc = music._session(guild_id).voice_client if music else None
        return vc if vc and vc.is_connected() else None

    def is_active(self, guild_id: int) -> bool:
        """True while Spotify is what the voice client is playing (or paused on)."""
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
            # Stop whatever another provider was playing, without advancing its queue.
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
                # Something else took over the voice client (another provider,
                # /stop, /go): Spotify for this server is done.
                self._sources.pop(gid, None)
                asyncio.run_coroutine_threadsafe(self.sessions.stop(gid), self.bot.loop)

        vc.play(source, after=after)
        if music:
            s = music._session(guild_id)
            s.is_playing = True
            s.start_time = time.time()
            s.paused_duration = 0.0
            s.last_paused_at = None

    def _on_event(self, session, etype: str, data: dict):
        guild_id = session.guild_id
        vc = self._voice_client(guild_id)
        music = self.music
        if etype == "metadata" and music:
            artists = data.get("artist_names") or []
            if isinstance(artists, list):
                artists = ", ".join(artists)
            s = music._session(guild_id)
            s.current_song_title = f"{data.get('name', 'Spotify')} - {artists}" if artists else data.get("name", "Spotify")
            s.current_song_data = None
            s.start_time = time.time() - (int(data.get("position") or 0) / 1000)
            s.paused_duration = 0.0
            s.last_paused_at = None
            asyncio.create_task(music.refresh_controller(guild_id))
        if not self.is_active(guild_id) or not vc:
            return
        # Follow pause / resume done from the Spotify app on someone's phone.
        if etype in ("paused", "stopped", "not_playing") and vc.is_playing():
            vc.pause()
        elif etype == "playing" and vc.is_paused():
            vc.resume()

    async def _idle_loop(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for gid, s in list(self.sessions.by_guild.items()):
                if not s.alive:
                    self._sources.pop(gid, None)
                    await self.sessions.stop(gid)
                elif not self._voice_client(gid) and now - s.last_activity > 60:
                    await self.sessions.stop(gid)
                elif (s.paused or not self.is_active(gid)) and now - s.last_activity > IDLE_SECONDS:
                    src = self._sources.pop(gid, None)
                    vc = self._voice_client(gid)
                    if vc and src is not None and vc.source is src:
                        vc.stop()
                    await self.sessions.stop(gid)

    # ------------------------------------------------ called by MusicCog ---

    async def play(self, interaction: discord.Interaction, query: str):
        """/play when the provider is Spotify (or a Spotify link was given)."""
        guild_id = interaction.guild.id
        vc = self._voice_client(guild_id)
        if not vc:
            return await interaction.followup.send("Use `/join` first so the bot is in your voice channel.")
        current = self.sessions.get(guild_id)
        # The account that's already playing here keeps playing (and searching),
        # so a second listener can queue songs without logging in.
        account = current.user_id if current else interaction.user.id
        if not current and not self.logged_in(account):
            return await interaction.followup.send(
                "This server plays from **Spotify**. Log in once with `/spotify login` (Spotify Premium needed), "
                "or pick another source with `/provider`."
            )
        try:
            uri = parse_spotify_link(query)
            info = None
            if uri and uri.startswith("spotify:track:"):
                info = await self.auth.track(account, uri.split(":")[-1])
            elif not uri:
                results = await self.auth.search(account, query, limit=1)
                if not results:
                    return await interaction.followup.send(f"Nothing found on Spotify for **{query}**.")
                info = results[0]
                uri = info["uri"]

            if current and self.is_active(guild_id) and not current.paused and uri.startswith("spotify:track:"):
                await current.call("/player/add_to_queue", {"uri": uri})
                return await interaction.followup.send(f"Queued: **{info['name']}** - {info['artists']}")

            session = await self.sessions.ensure(guild_id, account)
            session.on_event = self._on_event
            self._attach_audio(guild_id, vc, session)
            await session.play(uri)
        except SpotifyError as e:
            return await interaction.followup.send(str(e))
        except Exception:
            logger.exception("Spotify play failed")
            return await interaction.followup.send("Spotify couldn't play that. Try again.")

        if info:
            await interaction.followup.send(f"Now playing from Spotify: **{info['name']}** - {info['artists']}")
        else:
            await interaction.followup.send(f"Now playing this Spotify {uri.split(':')[1]}.")

    async def control(self, guild_id: int, action: str, seconds: float = 0) -> str:
        """pause / resume / next / prev / seek / stop for the Spotify player here."""
        s = self.sessions.get(guild_id)
        vc = self._voice_client(guild_id)
        if not s:
            return "Spotify isn't playing."
        try:
            if action == "pause":
                await s.call("/player/pause")
                if vc and vc.is_playing():
                    vc.pause()
                return "Paused."
            if action == "resume":
                await s.call("/player/resume")
                if vc and vc.is_paused():
                    vc.resume()
                return "Resumed."
            if action == "next":
                await s.call("/player/next")
                if vc and vc.is_paused():
                    vc.resume()
                return "Skipped to the next song."
            if action == "prev":
                await s.call("/player/prev")
                if vc and vc.is_paused():
                    vc.resume()
                return "Playing the previous song."
            if action == "seek":
                await s.call("/player/seek", {"position": int(seconds * 1000), "relative": True})
                return f"Moved {'forward' if seconds > 0 else 'back'} {abs(int(seconds))} seconds."
            if action == "stop":
                src = self._sources.pop(guild_id, None)
                if vc and src is not None and vc.source is src:
                    vc.stop()
                await self.sessions.stop(guild_id)
                return "Stopped."
        except SpotifyError as e:
            return str(e)
        return "Unknown action."

    async def link_player(self, interaction: discord.Interaction):
        """Step 2: link the audio player once via spotify.com/pair."""
        async def on_url(url: str):
            await interaction.followup.send(
                "**Last step: link the player** (one time).\n"
                f"1. Open {url}\n"
                "2. Log in with the **same** Spotify account if asked, and approve.\n"
                "I'll wait up to 10 minutes.",
                ephemeral=True, suppress_embeds=True,
            )
        try:
            await self.sessions.pair(interaction.user.id, on_url)
        except SpotifyError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        except Exception:
            logger.exception("Spotify player linking failed")
            return await interaction.followup.send("Linking failed. Run `/spotify login` to try again.", ephemeral=True)
        await interaction.followup.send(
            "Spotify is ready. Choose **Spotify** in `/provider`, then use `/play` as usual.", ephemeral=True
        )

    # ----------------------------------------------------------- commands ---

    @spotify.command(name="login", description="Connect your Spotify Premium account (one time)")
    async def login_cmd(self, interaction: discord.Interaction):
        if not find_librespot():
            return await interaction.response.send_message(
                "Spotify playback isn't installed on this bot's server yet. Ask the bot owner.", ephemeral=True
            )
        data = self.store.get(interaction.user.id)
        if data and self.sessions.is_paired(interaction.user.id):
            return await interaction.response.send_message(
                f"You're already connected as **{data.get('display_name', 'your account')}**. "
                "Choose **Spotify** in `/provider` and use `/play`. To switch accounts, use `/spotify logout` first.",
                ephemeral=True,
            )
        if data:
            # Step 1 is done; only the player still needs linking.
            await interaction.response.defer(ephemeral=True, thinking=True)
            return await self.link_player(interaction)
        await interaction.response.send_message(LOGIN_TEXT, view=LoginView(self), ephemeral=True,
                                                suppress_embeds=True)

    @spotify.command(name="logout", description="Remove your Spotify login from this bot")
    async def logout_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        for gid, s in list(self.sessions.by_guild.items()):
            if s.user_id == interaction.user.id:
                await self.control(gid, "stop")
        removed = self.auth.forget(interaction.user.id)
        await interaction.followup.send(
            "Your Spotify login was deleted from this bot." if removed else "You weren't logged in.",
            ephemeral=True,
        )
