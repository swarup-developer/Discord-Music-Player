import asyncio
import json
import logging
import os
import re
import time
from typing import Optional, List, Dict, Any

import discord
import httpx
from discord.ext import commands

from .jiosaavn import JioSaavnHandler
from .youtube import YouTubeHandler
from .soundcloud import SoundCloudHandler
from .spotify import SpotifyHandler
from .local_files import LocalFileHandler
from .tv import IPTVManager, CountrySelectView, IPTVChannelView
from .state import BotState, QueueItem
from .audio_source import JitterBuffer
from .search import search_songs

logger = logging.getLogger(__name__)


class QueueView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int, interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.interaction = interaction
        self.current_page = 0
        self.per_page = 10
        self._update_buttons()

    def _update_buttons(self):
        queue = self.cog.state.queue_for(self.guild_id)
        max_pages = max(1, ((len(queue) - 1) // self.per_page) + 1)
        self.prev_page.disabled = (self.current_page == 0)
        self.next_page.disabled = (self.current_page >= max_pages - 1)

    def _build_embed(self) -> discord.Embed:
        queue = self.cog.state.queue_for(self.guild_id)
        max_pages = max(1, ((len(queue) - 1) // self.per_page) + 1)
        
        embed = discord.Embed(
            title="🎶 Upcoming Music Queue", 
            color=discord.Color.from_rgb(114, 137, 218)
        )
        
        session = self.cog._session(self.guild_id)
        current_title = session.current_song_title or "Nothing playing"
        embed.add_field(name="📻 Now Playing", value=f"**{current_title}**", inline=False)
        
        queue_desc = []
        if not queue:
            queue_desc.append("*The queue is empty. Add more songs using `/play`!*")
        else:
            start = self.current_page * self.per_page
            end = start + self.per_page
            page_items = list(queue)[start:end]
            
            for i, item in enumerate(page_items, start + 1):
                requester_mention = f" — (Requested by <@{item.requested_by}>)" if item.requested_by else ""
                queue_desc.append(f"`{i:02d}.` **{item.title}**{requester_mention}")
        
        embed.description = "\n".join(queue_desc)
        embed.set_footer(text=f"Page {self.current_page + 1} of {max_pages} • {len(queue)} songs queued")
        return embed

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction.user.id:
            return await interaction.response.send_message("Only the command requester can paginate.", ephemeral=True)
        self.current_page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction.user.id:
            return await interaction.response.send_message("Only the command requester can paginate.", ephemeral=True)
        self.current_page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


class EQSelect(discord.ui.Select):
    def __init__(self, cog: "MusicCog", guild_id: int, current_preset: str):
        self.cog = cog
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label="Normal (EQ Off)", value="off", emoji="🎵", description="No audio filter applied"),
            discord.SelectOption(label="Bass Boost", value="bassboost", emoji="🔊", description="Enhanced deep bass frequencies"),
            discord.SelectOption(label="Vocal Booster", value="vocal", emoji="🗣️", description="Boost mids for clearer vocals"),
            discord.SelectOption(label="Treble Boost", value="treble", emoji="🎼", description="Crisp high frequencies"),
            discord.SelectOption(label="Lo-Fi", value="lofi", emoji="📻", description="Retro radio/telephone filter"),
            discord.SelectOption(label="Acoustic", value="acoustic", emoji="🎸", description="Warm, clear acoustic profile"),
        ]
        for opt in options:
            if opt.value == current_preset:
                opt.default = True
        super().__init__(placeholder="Select Equalizer (EQ) Preset...", min_values=1, max_values=1, options=options, row=2)

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild and self.cog.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.response.send_message(
                "This bot is already active in another server.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        preset = self.values[0]
        self.cog.state.set_eq_preset(self.guild_id, preset)
        await self.cog.reload_current_track(self.guild_id, interaction)
        await self.cog.refresh_controller(self.guild_id)


class LyricsView(discord.ui.View):
    def __init__(self, title: str, pages: list[str], interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.title = title
        self.pages = pages
        self.interaction = interaction
        self.current_page = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_page.disabled = (self.current_page == 0)
        self.next_page.disabled = (self.current_page >= len(self.pages) - 1)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"Lyrics: {self.title}", description=self.pages[self.current_page], color=discord.Color.gold())
        if len(self.pages) > 1:
            embed.set_footer(text=f"Page {self.current_page + 1} of {len(self.pages)}")
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction.user.id:
            return await interaction.response.send_message("Only the command requester can paginate.", ephemeral=True)
        self.current_page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction.user.id:
            return await interaction.response.send_message("Only the command requester can paginate.", ephemeral=True)
        self.current_page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


class ProviderSelectionView(discord.ui.View):
    def __init__(self, state: BotState, guild_id: int):
        super().__init__(timeout=60)
        self.state = state
        self.guild_id = guild_id
        
        current = self.state.get_provider(guild_id)
        
        yt_style = discord.ButtonStyle.primary if current == "youtube" else discord.ButtonStyle.secondary
        saavn_style = discord.ButtonStyle.primary if current == "jiosaavn" else discord.ButtonStyle.secondary
        sc_style = discord.ButtonStyle.primary if current == "soundcloud" else discord.ButtonStyle.secondary
        spotify_style = discord.ButtonStyle.primary if current == "spotify" else discord.ButtonStyle.secondary
        local_style = discord.ButtonStyle.primary if current == "local" else discord.ButtonStyle.secondary
        
        self.yt_btn = discord.ui.Button(label="YouTube", style=yt_style, custom_id="provider_youtube")
        self.yt_btn.callback = self.choose_youtube
        self.add_item(self.yt_btn)
        
        self.saavn_btn = discord.ui.Button(label="JioSaavn", style=saavn_style, custom_id="provider_jiosaavn")
        self.saavn_btn.callback = self.choose_jiosaavn
        self.add_item(self.saavn_btn)

        self.sc_btn = discord.ui.Button(label="SoundCloud", style=sc_style, custom_id="provider_soundcloud")
        self.sc_btn.callback = self.choose_soundcloud
        self.add_item(self.sc_btn)

        self.spotify_btn = discord.ui.Button(label="Spotify", style=spotify_style, custom_id="provider_spotify")
        self.spotify_btn.callback = self.choose_spotify
        self.add_item(self.spotify_btn)

        self.local_btn = discord.ui.Button(label="Local Files", style=local_style, custom_id="provider_local")
        self.local_btn.callback = self.choose_local
        self.add_item(self.local_btn)

    async def choose_youtube(self, interaction: discord.Interaction):
        self.state.set_provider(self.guild_id, "youtube")
        self.yt_btn.style = discord.ButtonStyle.success
        self.saavn_btn.style = discord.ButtonStyle.secondary
        self.sc_btn.style = discord.ButtonStyle.secondary
        self.spotify_btn.style = discord.ButtonStyle.secondary
        self.local_btn.style = discord.ButtonStyle.secondary
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Default music provider set to: **YouTube** 🎥", view=self)

    async def choose_jiosaavn(self, interaction: discord.Interaction):
        self.state.set_provider(self.guild_id, "jiosaavn")
        self.yt_btn.style = discord.ButtonStyle.secondary
        self.saavn_btn.style = discord.ButtonStyle.success
        self.sc_btn.style = discord.ButtonStyle.secondary
        self.spotify_btn.style = discord.ButtonStyle.secondary
        self.local_btn.style = discord.ButtonStyle.secondary
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Default music provider set to: **JioSaavn** 🎵", view=self)

    async def choose_soundcloud(self, interaction: discord.Interaction):
        self.state.set_provider(self.guild_id, "soundcloud")
        self.yt_btn.style = discord.ButtonStyle.secondary
        self.saavn_btn.style = discord.ButtonStyle.secondary
        self.sc_btn.style = discord.ButtonStyle.success
        self.spotify_btn.style = discord.ButtonStyle.secondary
        self.local_btn.style = discord.ButtonStyle.secondary
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Default music provider set to: **SoundCloud** 🧡", view=self)

    async def choose_spotify(self, interaction: discord.Interaction):
        self.state.set_provider(self.guild_id, "spotify")
        self.yt_btn.style = discord.ButtonStyle.secondary
        self.saavn_btn.style = discord.ButtonStyle.secondary
        self.sc_btn.style = discord.ButtonStyle.secondary
        self.spotify_btn.style = discord.ButtonStyle.success
        self.local_btn.style = discord.ButtonStyle.secondary
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Default music provider set to: **Spotify** 🟢", view=self)

    async def choose_local(self, interaction: discord.Interaction):
        self.state.set_provider(self.guild_id, "local")
        self.yt_btn.style = discord.ButtonStyle.secondary
        self.saavn_btn.style = discord.ButtonStyle.secondary
        self.sc_btn.style = discord.ButtonStyle.secondary
        self.spotify_btn.style = discord.ButtonStyle.secondary
        self.local_btn.style = discord.ButtonStyle.success
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Default music provider set to: **Local Files** 📁", view=self)


class RadioSelect(discord.ui.Select):
    def __init__(self, presets: List[Dict[str, str]]):
        options = [
            discord.SelectOption(label=p["name"], value=p["url"], description=f"Category: {p['category']}", emoji="📻")
            for p in presets
        ]
        super().__init__(
            placeholder="Select a radio station to play...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="radio_preset_select"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_url = self.values[0]
        preset_name = next((p["name"] for p in self.view.presets if p["url"] == selected_url), "Radio Station")
        
        music_cog = interaction.client.get_cog("MusicCog")
        if not music_cog:
            return
            
        session = music_cog._session(interaction.guild.id)
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send("You must be in a voice channel to play radio.", ephemeral=True)
            
        if not session.voice_client:
            voice_client = await interaction.user.voice.channel.connect()
            music_cog.state.set_active(voice_client, interaction.user.voice.channel.id, interaction.channel.id, interaction.guild.id)
            
        title = f"📻 Radio: {preset_name}"
        is_playing = session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused())
        
        if is_playing:
            queue = music_cog.state.queue_for(interaction.guild.id)
            queue.append(QueueItem(query=selected_url, title=title, requested_by=interaction.user.id))
            await interaction.followup.send(f"Queued Radio: **{preset_name}**")
        else:
            await music_cog._play_audio(selected_url, interaction, title=title)
            await interaction.followup.send(f"Now playing Radio: **{preset_name}**")

class RadioSelectView(discord.ui.View):
    def __init__(self, presets: List[Dict[str, str]], user_id: int):
        super().__init__(timeout=60.0)
        self.presets = presets
        self.user_id = user_id
        self.add_item(RadioSelect(presets))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the command starter can select.", ephemeral=True)
            return False
        return True


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state: BotState = bot.music_state
        self.voice_lock = asyncio.Lock()
        self.empty_voice_tasks: dict[int, asyncio.Task] = {}
        self.voice_join_cooldowns: dict[int, float] = {}

        # Load IPTV channels list in the background
        asyncio.create_task(IPTVManager.load_channels())

        # Clean up any leftover temp files in the system temp directory on startup
        try:
            import tempfile
            temp_dir = tempfile.gettempdir()
            for filename in os.listdir(temp_dir):
                if filename.startswith("tmp") and (filename.endswith(".mp3") or filename.endswith(".m4a")):
                    try:
                        path = os.path.join(temp_dir, filename)
                        os.remove(path)
                        logger.info(f"Cleaned up leftover temp file on startup: {path}")
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Failed to scan and clean temp directory on startup: {e}")

    def _session(self, guild_id: int):
        return self.state.session_for(guild_id)

    class SearchResultButton(discord.ui.Button):
        def __init__(self, cog: "MusicCog", song: dict, label: str):
            super().__init__(label=label, style=discord.ButtonStyle.secondary)
            self.cog = cog
            self.song = song

        async def callback(self, interaction: discord.Interaction):
            if not interaction.guild:
                return await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
            await interaction.response.defer(ephemeral=True)
            await self.cog._play_song_data(self.song, interaction)

    class SearchResultsView(discord.ui.View):
        def __init__(self, cog: "MusicCog", interaction: discord.Interaction, results: list[dict]):
            super().__init__(timeout=180)
            self.cog = cog
            self.interaction = interaction
            self.results = results[:10]

            for index, song in enumerate(self.results, 1):
                self.add_item(cog.SearchResultButton(cog, song, str(index)))

        async def on_timeout(self) -> None:
            for item in self.children:
                item.disabled = True
            try:
                await self.interaction.edit_original_response(view=self)
            except Exception:
                pass

    class MusicControllerView(discord.ui.View):
        def __init__(self, cog: "MusicCog", guild_id: int):
            super().__init__(timeout=None)
            self.cog = cog
            self.guild_id = guild_id

            # Synchronize loop button style & label
            loop_mode = cog.state.get_loop_mode(guild_id)
            if loop_mode == "song":
                self.repeat.label = "Loop: Song"
                self.repeat.style = discord.ButtonStyle.success
            elif loop_mode == "queue":
                self.repeat.label = "Loop: Queue"
                self.repeat.style = discord.ButtonStyle.success
            else:
                self.repeat.label = "Loop: Off"
                self.repeat.style = discord.ButtonStyle.secondary

            # Synchronize shuffle button style
            shuffle_enabled = cog.state.shuffle_enabled.get(guild_id, False)
            self.shuffle.style = discord.ButtonStyle.success if shuffle_enabled else discord.ButtonStyle.secondary

            # Synchronize play_pause button label & style
            session = cog._session(guild_id)
            vc = session.voice_client
            if vc and vc.is_connected() and vc.is_paused():
                self.play_pause.label = "Play"
                self.play_pause.style = discord.ButtonStyle.success
            elif vc and vc.is_connected() and vc.is_playing():
                self.play_pause.label = "Pause"
                self.play_pause.style = discord.ButtonStyle.danger
            else:
                self.play_pause.label = "Play"
                self.play_pause.style = discord.ButtonStyle.success

            # Add EQ Select Dropdown on Row 2
            current_preset = cog.state.get_eq_preset(guild_id)
            self.add_item(EQSelect(cog, guild_id, current_preset))

        def _song_label(self) -> str:
            title = self.cog._session(self.guild_id).current_song_title or "Nothing playing"
            return title

        @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, row=0)
        async def play_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
            session = self.cog._session(self.guild_id)
            vc = session.voice_client
            if not vc or not vc.is_connected():
                return await interaction.response.send_message("Nothing is connected.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            if vc.is_playing():
                vc.pause()
                import time
                session.last_paused_at = time.time()
                button.label = "Play"
                button.style = discord.ButtonStyle.success
            elif vc.is_paused():
                vc.resume()
                import time
                if getattr(session, "last_paused_at", None):
                    session.paused_duration = getattr(session, "paused_duration", 0.0) + (time.time() - session.last_paused_at)
                    session.last_paused_at = None
                button.label = "Pause"
                button.style = discord.ButtonStyle.danger
            else:
                # Stopped/idle. Try to play next in queue if available
                queue = self.cog.state.queue_for(self.guild_id)
                if queue:
                    await self.cog._play_next_in_queue(self.guild_id)
                else:
                    await interaction.followup.send("Queue is empty. Use `/play` to play a song.", ephemeral=True)
                    return
            await self.cog.refresh_controller(interaction.guild.id if interaction.guild else self.guild_id)

        @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=0)
        async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.guild:
                return await interaction.response.send_message("Server only.", ephemeral=True)
            session = self.cog._session(interaction.guild.id)
            if not session.history:
                return await interaction.response.send_message("No previous track available.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            await self.cog.play_previous(interaction.guild.id, interaction)

        @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=0)
        async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.guild:
                return await interaction.response.send_message("Server only.", ephemeral=True)
            vc = self.cog._session(interaction.guild.id).voice_client
            if not vc or not vc.is_connected():
                return await interaction.response.send_message("Nothing is connected.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            self.cog._session(interaction.guild.id).advance_queue_on_stop = True
            vc.stop()

        @discord.ui.button(label="Repeat", style=discord.ButtonStyle.success, row=1)
        async def repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.guild:
                return await interaction.response.send_message("Server only.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            current_mode = self.cog.state.get_loop_mode(interaction.guild.id)
            if current_mode == "off":
                next_mode = "song"
            elif current_mode == "song":
                next_mode = "queue"
            else:
                next_mode = "off"
            self.cog.state.set_loop_mode(interaction.guild.id, next_mode)
            await self.cog.refresh_controller(interaction.guild.id)

        @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, row=1)
        async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.guild:
                return await interaction.response.send_message("Server only.", ephemeral=True)
            queue = self.cog.state.queue_for(interaction.guild.id)
            if len(queue) < 2:
                return await interaction.response.send_message("Shuffle is only available when a playlist is queued.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            enabled = not self.cog.state.shuffle_enabled.get(interaction.guild.id, False)
            self.cog.state.shuffle_enabled[interaction.guild.id] = enabled
            button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
            await self.cog.refresh_controller(interaction.guild.id)

        @discord.ui.button(label="-", style=discord.ButtonStyle.secondary, row=1)
        async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.guild:
                return await interaction.response.send_message("Server only.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            current = self.cog.state.get_volume(interaction.guild.id)
            new_volume = max(0.0, round(current - 0.2, 2))
            self.cog.sync_voice_volume(interaction.guild.id, new_volume)
            await self.cog.refresh_controller(interaction.guild.id)

        @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=1)
        async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.guild:
                return await interaction.response.send_message("Server only.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            current = self.cog.state.get_volume(interaction.guild.id)
            new_volume = min(2.0, round(current + 0.2, 2))
            self.cog.sync_voice_volume(interaction.guild.id, new_volume)
            await self.cog.refresh_controller(interaction.guild.id)

    def _should_respond(self, interaction: discord.Interaction, is_join: bool = False) -> bool:
        user_voice = interaction.user.voice.channel if interaction.user.voice else None
        if not user_voice:
            return False
        if is_join:
            return True
        if not interaction.guild:
            return False
        session = self._session(interaction.guild.id)
        return session.voice_client is not None and user_voice.id == session.voice_channel_id

    async def _reset_voice_state(self, guild: discord.Guild) -> None:
        voice_client = guild.voice_client
        if voice_client:
            try:
                await voice_client.disconnect(force=True)
            except Exception as e:
                logger.warning(f"Failed to disconnect stale voice client: {e}")
        self.state.clear(guild.id)
        await asyncio.sleep(2.0)

    def _voice_join_cooldown_remaining(self, guild_id: int) -> float:
        expires_at = self.voice_join_cooldowns.get(guild_id)
        if not expires_at:
            return 0.0
        remaining = expires_at - asyncio.get_event_loop().time()
        if remaining <= 0:
            self.voice_join_cooldowns.pop(guild_id, None)
            return 0.0
        return remaining

    def _set_voice_join_cooldown(self, guild_id: int, seconds: float = 15.0) -> None:
        self.voice_join_cooldowns[guild_id] = asyncio.get_event_loop().time() + seconds

    def _voice_status_lines(self, interaction: discord.Interaction) -> list[str]:
        guild = interaction.guild
        if not guild:
            return ["This command can only be used in a server."]

        lines = []
        voice_client = guild.voice_client
        cooldown_remaining = self._voice_join_cooldown_remaining(guild.id)
        user_voice = interaction.user.voice.channel if interaction.user.voice else None

        lines.append(
            f"Bot voice: connected to `{voice_client.channel}`"
            if voice_client and voice_client.is_connected() and voice_client.channel
            else "Bot voice: not connected"
        )
        lines.append(f"Join cooldown: {int(cooldown_remaining) + 1}s remaining" if cooldown_remaining > 0 else "Join cooldown: none")
        lines.append(
            f"Your voice: connected to `{user_voice}`" if user_voice else "Your voice: not connected"
        )
        if voice_client and voice_client.is_connected() and user_voice and voice_client.channel and user_voice.id != voice_client.channel.id:
            lines.append("Status: bot is in a different voice channel than you.")
        if not discord.opus.is_loaded():
            lines.append("Warning: Opus is not loaded.")
        return lines

    def sync_voice_volume(self, guild_id: int, volume: float) -> None:
        self.state.set_volume(guild_id, volume)
        voice_client = self._session(guild_id).voice_client
        if not voice_client or not voice_client.is_connected():
            return
        source = getattr(voice_client, "source", None)
        logger.info(f"sync_voice_volume: updating volume to {volume}. Current source type: {type(source)}")
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = volume ** 2
            logger.info(f"sync_voice_volume: updated PCMVolumeTransformer volume to {source.volume}")
        else:
            logger.warning(f"sync_voice_volume: source is not a PCMVolumeTransformer: {type(source)}")

    def _controller_embed(self, guild_id: int) -> discord.Embed:
        session = self._session(guild_id)
        queue = self.state.queue_for(guild_id)
        volume = self.state.get_volume(guild_id)
        loop_mode = self.state.get_loop_mode(guild_id)
        shuffle = self.state.shuffle_enabled.get(guild_id, False)
        active_effects = self.state.get_effects(guild_id)
        eq_preset = self.state.get_eq_preset(guild_id)
        
        loop_str = "Off"
        if loop_mode == "song":
            loop_str = "Repeat Song"
        elif loop_mode == "queue":
            loop_str = "Repeat Queue"
            
        effects_str = ", ".join([e.capitalize() for e in active_effects]) if active_effects else "None"
        
        eq_labels = {
            "off": "Normal (Off)",
            "bassboost": "Bass Boost",
            "vocal": "Vocal Booster",
            "treble": "Treble Boost",
            "lofi": "Lo-Fi",
            "acoustic": "Acoustic"
        }
        eq_str = eq_labels.get(eq_preset, "Normal (Off)")
        
        title = session.current_song_title or "Nothing playing"
        status = "Playing" if session.is_playing else "Paused" if session.voice_client and session.voice_client.is_paused() else "Idle"
        embed = discord.Embed(title="Playback Controller", color=discord.Color.green())
        embed.add_field(name="Song", value=title, inline=False)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Volume", value=f"{int(volume * 100)}%", inline=True)
        embed.add_field(name="Queue", value=f"{len(queue)} queued", inline=True)
        embed.add_field(name="Loop", value=loop_str, inline=True)
        embed.add_field(name="Shuffle", value="On" if shuffle else "Off", inline=True)
        embed.add_field(name="Effects", value=effects_str, inline=True)
        embed.add_field(name="EQ Preset", value=eq_str, inline=True)
        return embed

    async def refresh_controller(self, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        session = self._session(guild_id)
        channel = guild.get_channel(session.linked_text_channel_id) if session.linked_text_channel_id else None
        if channel is None:
            return
        message_id = self.state.controller_message_id.get(guild_id)
        view = self.MusicControllerView(self, guild_id)
        # Hide shuffle when there is no playlist-like queue.
        if len(self.state.queue_for(guild_id)) < 2:
            for item in list(view.children):
                if getattr(item, "label", None) == "Shuffle":
                    item.disabled = True
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
            except Exception:
                pass
        try:
            message = await channel.send(embed=self._controller_embed(guild_id), view=view)
            self.state.controller_message_id[guild_id] = message.id
        except Exception as e:
            logger.warning(f"Failed to publish controller: {e}")

    async def _ensure_deferred(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        if interaction.response.is_done():
            return
        try:
            await interaction.response.defer(ephemeral=ephemeral)
        except discord.NotFound:
            logger.warning("Interaction expired before defer could be sent")
        except discord.HTTPException as e:
            logger.warning(f"Failed to defer interaction: {e}")

    def _stop_current_track_for_manual_replace(self, guild_id: int) -> None:
        session = self._session(guild_id)
        if not session.voice_client:
            return
        if session.voice_client.is_playing() or session.voice_client.is_paused():
            # Manual track replacement should not fall through to queue advancement.
            session.advance_queue_on_stop = False
            session.voice_client.stop()

    async def _get_audio_source_and_title(self, guild_id: int, query: str, volume: float, effects: Optional[list[str]], seek: Optional[float] = None, eq_preset: Optional[str] = None):
        if JioSaavnHandler.is_jiosaavn_url(query):
            return await JioSaavnHandler.get_audio_source(query, volume, effects=effects, seek=seek, eq_preset=eq_preset)
        elif YouTubeHandler.is_youtube_url(query):
            return await YouTubeHandler.get_audio_source(query, volume, effects=effects, seek=seek, eq_preset=eq_preset)
        elif SoundCloudHandler.is_soundcloud_url(query):
            return await SoundCloudHandler.get_audio_source(query, volume, effects=effects, seek=seek, eq_preset=eq_preset)
        elif os.path.exists(query) and os.path.isfile(query):
            # Local file playback
            try:
                ffmpeg_options = "-vn -flags low_delay"
                filters = []
                if eq_preset:
                    if eq_preset == "bassboost":
                        filters.append("equalizer=f=60:width_type=o:width=2:g=8")
                    elif eq_preset == "vocal":
                        filters.append("equalizer=f=1000:width_type=q:width=1:g=5,equalizer=f=3000:width_type=q:width=1:g=3")
                    elif eq_preset == "treble":
                        filters.append("equalizer=f=8000:width_type=o:width=2:g=6")
                    elif eq_preset == "lofi":
                        filters.append("highpass=f=300,lowpass=f=4000")
                    elif eq_preset == "acoustic":
                        filters.append("equalizer=f=120:width_type=o:width=2:g=3,equalizer=f=2000:width_type=o:width=2:g=2,equalizer=f=8000:width_type=o:width=2:g=3")

                if __name__ == "__main__" or effects:
                    if effects and "bassboost" in effects and eq_preset != "bassboost":
                        filters.append("equalizer=f=60:width_type=o:width=2:g=8")
                    if effects and "nightcore" in effects:
                        filters.append("asetrate=48000*1.25")
                filters.append("aresample=osr=48000:osf=s16")
                ffmpeg_options += f' -af "{",".join(filters)}"'

                ffmpeg_before_options = "-nostdin -probesize 100000 -analyzeduration 100000 -fflags nobuffer"
                if seek and seek > 0:
                    ffmpeg_before_options = f"-ss {seek} " + ffmpeg_before_options

                audio_source = discord.FFmpegPCMAudio(query, before_options=ffmpeg_before_options, options=ffmpeg_options)
                title = os.path.basename(query)
                return audio_source, title
            except Exception as e:
                logger.error(f"Failed to play local file: {e}")
                return None, None
        elif query.startswith("http://") or query.startswith("https://"):
            ffmpeg_options = "-vn -flags low_delay"
            filters = []
            if eq_preset:
                if eq_preset == "bassboost":
                    filters.append("equalizer=f=60:width_type=o:width=2:g=8")
                elif eq_preset == "vocal":
                    filters.append("equalizer=f=1000:width_type=q:width=1:g=5,equalizer=f=3000:width_type=q:width=1:g=3")
                elif eq_preset == "treble":
                    filters.append("equalizer=f=8000:width_type=o:width=2:g=6")
                elif eq_preset == "lofi":
                    filters.append("highpass=f=300,lowpass=f=4000")
                elif eq_preset == "acoustic":
                    filters.append("equalizer=f=120:width_type=o:width=2:g=3,equalizer=f=2000:width_type=o:width=2:g=2,equalizer=f=8000:width_type=o:width=2:g=3")

            if effects:
                if "bassboost" in effects and eq_preset != "bassboost":
                    filters.append("equalizer=f=60:width_type=o:width=2:g=8")
                if "nightcore" in effects:
                    filters.append("asetrate=48000*1.25")
            filters.append("aresample=osr=48000:osf=s16")
            ffmpeg_options += f' -af "{",".join(filters)}"'
            
            ffmpeg_before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin -probesize 100000 -analyzeduration 100000 -fflags nobuffer"
            if seek and seek > 0:
                ffmpeg_before_options = f"-ss {seek} " + ffmpeg_before_options
            try:
                audio_source = discord.FFmpegPCMAudio(query, before_options=ffmpeg_before_options, options=ffmpeg_options)
                title = query.split("/")[-1] or "Direct Stream"
                title = title.split("?")[0]
                return audio_source, title
            except Exception as e:
                logger.error(f"Failed to load direct stream URL: {e}")
                return None, None
        else:
            provider = self.state.get_provider(guild_id)
            if provider == "youtube" or provider == "spotify":
                return await YouTubeHandler.get_audio_source(query, volume, effects=effects, seek=seek, eq_preset=eq_preset)
            elif provider == "soundcloud":
                return await SoundCloudHandler.get_audio_source(query, volume, effects=effects, seek=seek, eq_preset=eq_preset)
            elif provider == "local":
                from .local_files import LocalFileHandler
                files = LocalFileHandler.list_files()
                for f in files:
                    if query.lower() in f.lower():
                        abs_path = LocalFileHandler.get_absolute_path(f)
                        if abs_path:
                            return await self._get_audio_source_and_title(guild_id, abs_path, volume, effects=effects, seek=seek, eq_preset=eq_preset)
                return None, None
            else:
                return await JioSaavnHandler.get_audio_source(query, volume, effects=effects, seek=seek, eq_preset=eq_preset)

    async def _get_audio_source_for_song(self, song: dict, volume: float, effects: Optional[list[str]], seek: Optional[float] = None, eq_preset: Optional[str] = None):
        if song.get("provider") == "youtube":
            return await YouTubeHandler.get_audio_source_for_song(song, volume, effects=effects, seek=seek, eq_preset=eq_preset)
        elif song.get("provider") == "soundcloud":
            return await SoundCloudHandler.get_audio_source_for_song(song, volume, effects=effects, seek=seek, eq_preset=eq_preset)
        elif song.get("provider") == "local":
            audio_source, _ = await self._get_audio_source_and_title(
                guild_id=0,
                query=song.get("url"),
                volume=volume,
                effects=effects,
                seek=seek,
                eq_preset=eq_preset
            )
            return audio_source
        else:
            return await JioSaavnHandler.get_audio_source_for_song(song, volume, effects=effects, seek=seek, eq_preset=eq_preset)

    async def _play_startup_sound(self, guild_id: int):
        session = self._session(guild_id)
        if not session.voice_client or not session.voice_client.is_connected():
            return

        sound_path = "audio/audio.mp3"
        if os.path.exists(sound_path):
            try:
                if session.voice_client.is_playing() or session.voice_client.is_paused():
                    return

                volume = self.state.get_volume(guild_id)
                raw_source = discord.FFmpegPCMAudio(sound_path, options="-vn")
                audio_source = discord.PCMVolumeTransformer(raw_source, volume=volume ** 2)

                session.is_playing = True
                session.current_song_title = "Startup Sound"

                def after_startup(error):
                    if error:
                        logger.error(f"Startup sound playback error: {error}")
                    session.is_playing = False
                    session.current_song_title = None
                    asyncio.run_coroutine_threadsafe(self._play_next_in_queue(guild_id), self.bot.loop)
                    asyncio.run_coroutine_threadsafe(self.refresh_controller(guild_id), self.bot.loop)

                session.voice_client.play(audio_source, after=after_startup)
                await self.refresh_controller(guild_id)
            except Exception as e:
                logger.error(f"Failed to play startup sound: {e}")

    async def _play_audio(self, query: str, interaction: discord.Interaction, title: Optional[str] = None):
        if not interaction.guild:
            return await interaction.followup.send("This command can only be used in a server.")
        
        # Enforce provider check for playback
        provider = self.state.get_provider(interaction.guild.id)
        if JioSaavnHandler.is_jiosaavn_url(query) and provider == "youtube":
            search_query = title
            if not search_query or search_query == "Unknown":
                search_query = query
            await interaction.followup.send(f"Switched provider to YouTube. Searching for **{search_query}**...")
            results = await search_songs(search_query, "youtube", limit=1)
            if results:
                song = results[0]
                song["provider"] = "youtube"
                await self._play_song_data(song, interaction)
            else:
                await interaction.followup.send(f"No results found on YouTube for **{search_query}**. Skipping.")
                asyncio.run_coroutine_threadsafe(self._play_next_in_queue(interaction.guild.id), self.bot.loop)
            return
        if YouTubeHandler.is_youtube_url(query) and provider == "jiosaavn":
            search_query = title
            if not search_query or search_query == "Unknown":
                meta = await YouTubeHandler.extract_stream_url_async(query)
                if meta:
                    search_query = meta[1]
                else:
                    search_query = query
            await interaction.followup.send(f"Switched provider to JioSaavn. Searching for **{search_query}**...")
            results = await search_songs(search_query, "jiosaavn", limit=1)
            if results:
                song = results[0]
                song["provider"] = "jiosaavn"
                await self._play_song_data(song, interaction)
            else:
                await interaction.followup.send(f"No results found on JioSaavn for **{search_query}**. Skipping.")
                asyncio.run_coroutine_threadsafe(self._play_next_in_queue(interaction.guild.id), self.bot.loop)
            return
        if (JioSaavnHandler.is_jiosaavn_url(query) or YouTubeHandler.is_youtube_url(query)) and provider == "soundcloud":
            search_query = title
            if not search_query or search_query == "Unknown":
                if YouTubeHandler.is_youtube_url(query):
                    meta = await YouTubeHandler.extract_stream_url_async(query)
                    search_query = meta[1] if meta else query
                else:
                    search_query = query
            await interaction.followup.send(f"Switched provider to SoundCloud. Searching for **{search_query}**...")
            results = await search_songs(search_query, "soundcloud", limit=1)
            if results:
                song = results[0]
                song["provider"] = "soundcloud"
                await self._play_song_data(song, interaction)
            else:
                await interaction.followup.send(f"No results found on SoundCloud for **{search_query}**. Skipping.")
                asyncio.run_coroutine_threadsafe(self._play_next_in_queue(interaction.guild.id), self.bot.loop)
            return

        session = self._session(interaction.guild.id)
        if not session.voice_client or not session.voice_client.is_connected():
            return await interaction.followup.send("Join a voice channel first!")

        self._stop_current_track_for_manual_replace(interaction.guild.id)

        volume = self.state.get_volume(interaction.guild.id)
        effects = self.state.get_effects(interaction.guild.id)
        eq_preset = self.state.get_eq_preset(interaction.guild.id)
        audio_source, title = await self._get_audio_source_and_title(interaction.guild.id, query, volume, effects=effects, eq_preset=eq_preset)
        if not audio_source:
            return await interaction.followup.send(f"Failed to play: **{query}**")
        audio_source = discord.PCMVolumeTransformer(JitterBuffer(audio_source), volume=volume ** 2)
        
        # Resolve temp_file_path recursively
        src = audio_source
        while hasattr(src, "original"):
            src = src.original
        session.temp_file_path = getattr(src, "temp_file_path", None)
        session.start_time = time.time()
        session.paused_duration = 0.0
        session.last_paused_at = None
        guild_id = interaction.guild.id

        def after_playback(error):
            if error:
                logger.error(f"Playback error: {error}")
            session.is_playing = False
            session.current_song_title = None
            session.start_time = 0.0
            session.paused_duration = 0.0
            session.last_paused_at = None
            if session.temp_file_path:
                try:
                    if os.path.exists(session.temp_file_path):
                        os.remove(session.temp_file_path)
                except Exception:
                    pass
                session.temp_file_path = None
            
            old_data = session.current_song_data
            if old_data:
                if not session.history or session.history[-1] != old_data:
                    session.history.append(old_data)
                    if len(session.history) > 20:
                        session.history.pop(0)
                session.current_song_data = None

            if session.advance_queue_on_stop:
                asyncio.run_coroutine_threadsafe(self._play_next_in_queue(guild_id, old_data), self.bot.loop)
            session.advance_queue_on_stop = True
            asyncio.run_coroutine_threadsafe(self.refresh_controller(guild_id), self.bot.loop)

            # Instant memory reclamation
            import gc
            import ctypes
            gc.collect()
            try:
                libc = ctypes.CDLL("libc.so.6")
                libc.malloc_trim(0)
            except Exception:
                pass

        session.voice_client.play(audio_source, after=after_playback)
        session.is_playing = True
        session.current_song_title = title
        session.current_song_data = {"type": "query", "query": query, "title": title}
        await interaction.followup.send(f"Now playing: **{title}**")
        await self.refresh_controller(interaction.guild.id)

    async def _play_song_data(self, song: dict, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.followup.send("This command can only be used in a server.")
        
        # Enforce provider check for playback
        provider = self.state.get_provider(interaction.guild.id)
        if song.get("provider") != provider and provider != "spotify":
            song_name = song.get("song") or song.get("title") or "Unknown Song"
            if provider == "jiosaavn":
                await interaction.followup.send(f"Switched provider to JioSaavn. Searching for **{song_name}**...")
                results = await search_songs(song_name, "jiosaavn", limit=1)
                if results:
                    new_song = results[0]
                    new_song["provider"] = "jiosaavn"
                    await self._play_song_data(new_song, interaction)
                else:
                    await interaction.followup.send(f"No results found on JioSaavn for **{song_name}**. Skipping.")
                    asyncio.run_coroutine_threadsafe(self._play_next_in_queue(interaction.guild.id), self.bot.loop)
                return
            elif provider == "youtube":
                await interaction.followup.send(f"Switched provider to YouTube. Searching for **{song_name}**...")
                results = await search_songs(song_name, "youtube", limit=1)
                if results:
                    new_song = results[0]
                    new_song["provider"] = "youtube"
                    await self._play_song_data(new_song, interaction)
                else:
                    await interaction.followup.send(f"No results found on YouTube for **{song_name}**. Skipping.")
                    asyncio.run_coroutine_threadsafe(self._play_next_in_queue(interaction.guild.id), self.bot.loop)
                return
            elif provider == "soundcloud":
                await interaction.followup.send(f"Switched provider to SoundCloud. Searching for **{song_name}**...")
                results = await search_songs(song_name, "soundcloud", limit=1)
                if results:
                    new_song = results[0]
                    new_song["provider"] = "soundcloud"
                    await self._play_song_data(new_song, interaction)
                else:
                    await interaction.followup.send(f"No results found on SoundCloud for **{song_name}**. Skipping.")
                    asyncio.run_coroutine_threadsafe(self._play_next_in_queue(interaction.guild.id), self.bot.loop)
                return
            elif provider == "local":
                await interaction.followup.send(f"Switched provider to Local Files. Searching for **{song_name}**...")
                results = await search_songs(song_name, "local", limit=1)
                if results:
                    new_song = results[0]
                    new_song["provider"] = "local"
                    await self._play_song_data(new_song, interaction)
                else:
                    await interaction.followup.send(f"No local files matching **{song_name}** found. Skipping.")
                    asyncio.run_coroutine_threadsafe(self._play_next_in_queue(interaction.guild.id), self.bot.loop)
                return
            else:
                await interaction.followup.send(f"Skipped **{song_name}**: belongs to {song.get('provider')} (active is {provider}).")
                asyncio.run_coroutine_threadsafe(self._play_next_in_queue(interaction.guild.id), self.bot.loop)
                return

        session = self._session(interaction.guild.id)
        if not session.voice_client or not session.voice_client.is_connected():
            return await interaction.followup.send("Join a voice channel first!")

        self._stop_current_track_for_manual_replace(interaction.guild.id)

        volume = self.state.get_volume(interaction.guild.id)
        effects = self.state.get_effects(interaction.guild.id)
        eq_preset = self.state.get_eq_preset(interaction.guild.id)
        audio_source, title = await self._get_audio_source_for_song(song, volume, effects=effects, eq_preset=eq_preset)
        if not audio_source:
            return await interaction.followup.send(f"Failed to play: **{song.get('song') or song.get('title') or 'Unknown'}**")
        audio_source = discord.PCMVolumeTransformer(JitterBuffer(audio_source), volume=volume ** 2)
        
        # Resolve temp_file_path recursively
        src = audio_source
        while hasattr(src, "original"):
            src = src.original
        session.temp_file_path = getattr(src, "temp_file_path", None)
        session.start_time = time.time()
        session.paused_duration = 0.0
        session.last_paused_at = None
        guild_id = interaction.guild.id

        def after_playback(error):
            if error:
                logger.error(f"Playback error: {error}")
            session.is_playing = False
            session.current_song_title = None
            session.start_time = 0.0
            session.paused_duration = 0.0
            session.last_paused_at = None
            if session.temp_file_path:
                try:
                    if os.path.exists(session.temp_file_path):
                        os.remove(session.temp_file_path)
                except Exception:
                    pass
                session.temp_file_path = None
            
            old_data = session.current_song_data
            if old_data:
                if not session.history or session.history[-1] != old_data:
                    session.history.append(old_data)
                    if len(session.history) > 20:
                        session.history.pop(0)
                session.current_song_data = None

            if session.advance_queue_on_stop:
                asyncio.run_coroutine_threadsafe(self._play_next_in_queue(guild_id, old_data), self.bot.loop)
            session.advance_queue_on_stop = True
            asyncio.run_coroutine_threadsafe(self.refresh_controller(guild_id), self.bot.loop)

            # Instant memory reclamation
            import gc
            import ctypes
            gc.collect()
            try:
                libc = ctypes.CDLL("libc.so.6")
                libc.malloc_trim(0)
            except Exception:
                pass

        session.voice_client.play(audio_source, after=after_playback)
        session.is_playing = True
        session.current_song_title = title
        session.current_song_data = {"type": "song", "song": song, "title": title}
        await interaction.followup.send(f"Now playing: **{title}**")
        await self.refresh_controller(interaction.guild.id)

    async def _enqueue_or_play(self, query: str, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.followup.send("This command can only be used in a server.")
        session = self._session(interaction.guild.id)
        queue = self.state.queue_for(interaction.guild.id)
        if session.voice_client and session.voice_client.is_connected() and session.voice_client.is_playing():
            queue.append(QueueItem(query=query, requested_by=interaction.user.id))
            return await interaction.followup.send(f"Queued: **{query}**")
        if session.voice_client and session.voice_client.is_connected() and session.voice_client.is_paused():
            queue.appendleft(QueueItem(query=query, requested_by=interaction.user.id))
            session.voice_client.resume()
            return await interaction.followup.send(f"Queued and resumed: **{query}**")
        await self._play_audio(query, interaction)

    async def _play_next_in_queue(self, guild_id: int, last_song_data: Optional[dict] = None):
        session = self._session(guild_id)
        if not session.voice_client or not session.voice_client.is_connected():
            return
        
        loop_mode = self.state.get_loop_mode(guild_id)
        queue = self.state.queue_for(guild_id)
        
        next_item = None
        
        if loop_mode == "song" and last_song_data:
            if last_song_data["type"] == "query":
                next_item = QueueItem(query=last_song_data["query"], title=last_song_data["title"])
            else:
                next_item = QueueItem(query=last_song_data["song"].get("id"), title=last_song_data["title"])
        elif not queue:
            return
        else:
            next_item = queue.popleft()
            if loop_mode == "queue" and last_song_data:
                if last_song_data["type"] == "query":
                    old_item = QueueItem(query=last_song_data["query"], title=last_song_data["title"])
                else:
                    old_item = QueueItem(query=last_song_data["song"].get("id"), title=last_song_data["title"])
                queue.append(old_item)

        guild = self.bot.get_guild(guild_id)
        if not guild or session.linked_text_channel_id is None:
            return
        channel = guild.get_channel(session.linked_text_channel_id)
        if channel is None:
            return

        class _FollowupProxy:
            def __init__(self, target_channel):
                self.target_channel = target_channel

            async def send(self, content=None, **kwargs):
                return await self.target_channel.send(content=content, **kwargs)

        proxy = type("ProxyInteraction", (), {})()
        proxy.guild = guild
        proxy.followup = _FollowupProxy(channel)
        
        if loop_mode == "song" and last_song_data:
            if last_song_data["type"] == "query":
                await self._play_audio(last_song_data["query"], proxy, title=last_song_data.get("title"))
            else:
                await self._play_song_data(last_song_data["song"], proxy)
        else:
            await self._play_audio(next_item.query, proxy, title=next_item.title)

    async def play_previous(self, guild_id: int, interaction: discord.Interaction):
        session = self._session(guild_id)
        if not session.history:
            return await interaction.followup.send("No previous track available.", ephemeral=True)

        prev_track = session.history.pop()

        if session.current_song_data:
            queue = self.state.queue_for(guild_id)
            if session.current_song_data["type"] == "query":
                item = QueueItem(query=session.current_song_data["query"], title=session.current_song_data["title"])
            else:
                item = QueueItem(query=session.current_song_data["song"].get("id"), title=session.current_song_data["title"])
            queue.appendleft(item)

        self._stop_current_track_for_manual_replace(guild_id)
        session.current_song_data = None

        if prev_track["type"] == "query":
            await self._play_audio(prev_track["query"], interaction, title=prev_track.get("title"))
        else:
            await self._play_song_data(prev_track["song"], interaction)

    async def reload_current_track(self, guild_id: int, interaction: discord.Interaction):
        session = self._session(guild_id)
        if not session.current_song_data:
            return
        
        # Calculate elapsed playback time for seeking
        elapsed = 0.0
        if session.start_time > 0:
            paused = session.paused_duration or 0.0
            if session.last_paused_at:
                paused += time.time() - session.last_paused_at
            elapsed = time.time() - session.start_time - paused
            elapsed = max(0.0, elapsed)

        current_data = session.current_song_data
        session.current_song_data = None
        session.advance_queue_on_stop = False
        if session.voice_client:
            session.voice_client.stop()
            
        await asyncio.sleep(0.5)

        volume = self.state.get_volume(guild_id)
        effects = self.state.get_effects(guild_id)
        eq_preset = self.state.get_eq_preset(guild_id)

        if current_data["type"] == "query":
            audio_source, title = await self._get_audio_source_and_title(guild_id, current_data["query"], volume, effects=effects, seek=elapsed, eq_preset=eq_preset)
        else:
            audio_source, title = await self._get_audio_source_for_song(current_data["song"], volume, effects=effects, seek=elapsed, eq_preset=eq_preset)

        if not audio_source:
            logger.warning(f"reload_current_track: failed to get audio source for guild {guild_id}")
            return

        audio_source = discord.PCMVolumeTransformer(JitterBuffer(audio_source), volume=volume ** 2)
        
        # Resolve temp_file_path recursively
        src = audio_source
        while hasattr(src, "original"):
            src = src.original
        session.temp_file_path = getattr(src, "temp_file_path", None)
        
        session.start_time = time.time() - elapsed
        session.paused_duration = 0.0
        session.last_paused_at = None
        session.current_song_data = current_data

        def after_reload(error):
            if error:
                logger.error(f"Reload playback error: {error}")
            session.is_playing = False
            session.current_song_title = None
            session.start_time = 0.0
            session.paused_duration = 0.0
            session.last_paused_at = None
            old_data = session.current_song_data
            if old_data:
                if not session.history or session.history[-1] != old_data:
                    session.history.append(old_data)
                session.current_song_data = None
            if session.advance_queue_on_stop:
                asyncio.run_coroutine_threadsafe(self._play_next_in_queue(guild_id, old_data), self.bot.loop)
            session.advance_queue_on_stop = True
            asyncio.run_coroutine_threadsafe(self.refresh_controller(guild_id), self.bot.loop)

        if session.voice_client and session.voice_client.is_connected():
            session.voice_client.play(audio_source, after=after_reload)
            session.is_playing = True
            session.current_song_title = title
            asyncio.run_coroutine_threadsafe(self.refresh_controller(guild_id), self.bot.loop)

    async def _cleanup_if_empty(self, guild: discord.Guild) -> None:
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected() or not voice_client.channel:
            return
        members = [member for member in voice_client.channel.members if not member.bot]
        guild_id = guild.id

        if members:
            pending = self.empty_voice_tasks.pop(guild_id, None)
            if pending and not pending.done():
                pending.cancel()
            return

        if guild_id in self.empty_voice_tasks and not self.empty_voice_tasks[guild_id].done():
            return

        async def _delayed_disconnect():
            disconnected = False
            try:
                # Wait 60 seconds then verify channel is still empty
                await asyncio.sleep(60)
                vc = guild.voice_client
                if not vc or not vc.is_connected() or not vc.channel:
                    return
                remaining = [member for member in vc.channel.members if not member.bot]
                if remaining:
                    # Users rejoined during the wait — cancel disconnect
                    return
                logger.info(f"Auto-disconnecting from empty voice channel {vc.channel.id}")
                disconnected = True
                await vc.disconnect(force=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"Auto-disconnect failed: {e}")
            finally:
                if disconnected and self.empty_voice_tasks.get(guild_id) is task:
                    self.state.clear(guild_id)
                    self.state.clear_queue(guild_id)
                self.empty_voice_tasks.pop(guild_id, None)

        task = asyncio.create_task(_delayed_disconnect())
        self.empty_voice_tasks[guild_id] = task

    def _build_results_embed(self, query: str, results: list[dict]) -> discord.Embed:
        embed = discord.Embed(title=f"Search Results: {query}", color=discord.Color.blue())
        embed.description = "\n".join(
            f"{i}. **{JioSaavnHandler.engine.format_string(r.get('song') or r.get('title'))}** - *{JioSaavnHandler.engine.format_string(r.get('album'))}*"
            for i, r in enumerate(results[:10], 1)
        )
        embed.set_footer(text="Click a number button below to play that result")
        return embed

    async def _resolve_and_play(self, query: str, interaction: discord.Interaction):
        if not interaction.guild:
            return
        
        guild_id = interaction.guild.id
        session = self._session(guild_id)
        queue = self.state.queue_for(guild_id)
        provider = self.state.get_provider(guild_id)
        
        # Check Spotify first before other URL logic
        if SpotifyHandler.is_spotify_url(query):
            await self._ensure_deferred(interaction)
            meta = await SpotifyHandler.resolve_url(query)
            if not meta or not meta["tracks"]:
                return await interaction.followup.send("Failed to resolve Spotify track/playlist metadata.")
                
            tracks = meta["tracks"]
            title = meta["name"]
            
            # Connect to voice if not already connected
            if not interaction.user.voice or not interaction.user.voice.channel:
                return await interaction.followup.send("You must be in a voice channel to play music.", ephemeral=True)
            
            if not session.voice_client:
                voice_client = await interaction.user.voice.channel.connect()
                self.state.set_active(voice_client, interaction.user.voice.channel.id, interaction.channel.id, guild_id)
            
            # If it's a single track
            if len(tracks) == 1:
                track = tracks[0]
                search_query = f"{track['title']} {track['artist']}"
                results = await search_songs(search_query, provider, limit=1)
                if not results:
                    return await interaction.followup.send(f"Resolved Spotify link, but found no matches on {provider.capitalize()} for '{search_query}'.")
                song = results[0]
                song_title = song.get("song") or song.get("title") or track['title']
                if provider == "jiosaavn":
                    song_title = JioSaavnHandler.engine.format_string(song_title)
                song_query = song.get("url") or song.get("id") or search_query
                
                is_playing = session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused())
                if is_playing:
                    queue.append(QueueItem(query=song_query, title=song_title, requested_by=interaction.user.id))
                    return await interaction.followup.send(f"Queued: **{song_title}** (from Spotify track)")
                else:
                    await self._play_song_data(song, interaction)
                    return
            else:
                # If it's an album or playlist
                is_playing = session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused())
                
                # Resolve first track immediately
                first_track = tracks[0]
                first_search = f"{first_track['title']} {first_track['artist']}"
                first_results = await search_songs(first_search, provider, limit=1)
                if not first_results:
                    first_query = first_search
                    first_title = first_track['title']
                else:
                    first_song = first_results[0]
                    first_query = first_song.get("url") or first_song.get("id")
                    first_title = first_song.get("song") or first_song.get("title") or first_track['title']
                    if provider == "jiosaavn":
                        first_title = JioSaavnHandler.engine.format_string(first_title)
                
                if is_playing:
                    queue.append(QueueItem(query=first_query, title=first_title, requested_by=interaction.user.id))
                else:
                    await self._play_audio(first_query, interaction, title=first_title)
                
                # Enqueue the rest of the tracks as search queries
                for track in tracks[1:]:
                    search_query = f"{track['title']} {track['artist']}"
                    queue.append(QueueItem(query=search_query, title=track['title'], requested_by=interaction.user.id))
                
                await interaction.followup.send(f"Loaded Spotify {meta['type']}: **{title}** (enqueued {len(tracks)} songs).")
                await self.refresh_controller(guild_id)
                return

        is_url = query.startswith("http://") or query.startswith("https://")
        abs_path = LocalFileHandler.get_absolute_path(query)
        is_local_file = abs_path is not None
        
        if is_url:
            if YouTubeHandler.is_youtube_url(query) and not YouTubeHandler.is_playable_youtube_url(query):
                return await interaction.followup.send(
                    "That YouTube link is not a playable video URL. "
                    "Please provide a valid YouTube watch, youtu.be, short, or playlist link."
                )

            # Playlist URL support
            if YouTubeHandler.is_youtube_url(query) and ("list=" in query or "playlist" in query):
                loop = asyncio.get_event_loop()
                playlist_items = await loop.run_in_executor(None, YouTubeHandler.extract_playlist_videos, query)
                if not playlist_items:
                    return await interaction.followup.send("Failed to extract any videos from the playlist.")
                
                first_item = playlist_items[0]
                first_url = first_item['url']
                first_title = first_item['title']
                
                is_playing = session.voice_client and session.voice_client.is_connected() and (session.voice_client.is_playing() or session.voice_client.is_paused())
                
                if is_playing:
                    queue.append(QueueItem(query=first_url, title=first_title, requested_by=interaction.user.id))
                else:
                    await self._play_audio(first_url, interaction, title=first_title)
                
                for item in playlist_items[1:]:
                    queue.append(QueueItem(query=item['url'], title=item['title'], requested_by=interaction.user.id))
                
                await interaction.followup.send(f"Loaded YouTube playlist: enqueued **{len(playlist_items)}** songs.")
                await self.refresh_controller(guild_id)
                return

            if session.voice_client and session.voice_client.is_connected() and (session.voice_client.is_playing() or session.voice_client.is_paused()):
                title = query
                if YouTubeHandler.is_youtube_url(query):
                    meta = await YouTubeHandler.extract_stream_url_async(query)
                    if meta:
                        title = meta[1]
                elif JioSaavnHandler.is_jiosaavn_url(query):
                    token = JioSaavnHandler.extract_token_from_url(query)
                    if token:
                        details = await JioSaavnHandler.engine.get_song_details_by_token(token)
                        if details:
                            title = JioSaavnHandler.engine.format_string(details.get("song") or details.get("title") or query)
                elif SoundCloudHandler.is_soundcloud_url(query):
                    meta = await SoundCloudHandler.extract_stream_url_async(query)
                    if meta:
                        title = meta[1]
                queue.append(QueueItem(query=query, title=title, requested_by=interaction.user.id))
                return await interaction.followup.send(f"Queued: **{title}**")
            else:
                await self._play_audio(query, interaction)
        elif is_local_file:
            # Connect to voice if not already connected
            if not interaction.user.voice or not interaction.user.voice.channel:
                return await interaction.followup.send("You must be in a voice channel to play local files.", ephemeral=True)
            
            if not session.voice_client:
                voice_client = await interaction.user.voice.channel.connect()
                self.state.set_active(voice_client, interaction.user.voice.channel.id, interaction.channel.id, guild_id)

            title = f"📁 Local: {os.path.basename(abs_path)}"
            is_playing = session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused())
            
            if is_playing:
                queue.append(QueueItem(query=abs_path, title=title, requested_by=interaction.user.id))
                return await interaction.followup.send(f"Queued Local File: **{os.path.basename(abs_path)}**")
            else:
                await self._play_audio(abs_path, interaction, title=title)
                return await interaction.followup.send(f"Now playing local file: **{os.path.basename(abs_path)}**")
        else:
            if provider == "youtube":
                results = await search_songs(query, "youtube", limit=1)
                if not results:
                    return await interaction.followup.send(f"No results found on YouTube for '{query}'")
                song = results[0]
            elif provider == "soundcloud":
                results = await search_songs(query, "soundcloud", limit=1)
                if not results:
                    return await interaction.followup.send(f"No results found on SoundCloud for '{query}'")
                song = results[0]
            elif provider == "local":
                results = await search_songs(query, "local", limit=1)
                if not results:
                    return await interaction.followup.send(f"No results found in Local Files for '{query}'")
                song = results[0]
            elif provider == "spotify":
                results = await search_songs(query, "youtube", limit=1)
                if not results:
                    return await interaction.followup.send(f"No results found on YouTube (Spotify fallback) for '{query}'")
                song = results[0]
            else:
                results = await search_songs(query, "jiosaavn", limit=1)
                if not results:
                    return await interaction.followup.send(f"No results found on JioSaavn for '{query}'")
                song = results[0]
                
            title = song.get("song") or song.get("title") or "Unknown"
            if provider == "jiosaavn":
                title = JioSaavnHandler.engine.format_string(title)
            song_query = song.get("url") or song.get("id") or query
            
            if session.voice_client and session.voice_client.is_connected() and (session.voice_client.is_playing() or session.voice_client.is_paused()):
                queue.append(QueueItem(query=song_query, title=title, requested_by=interaction.user.id))
                return await interaction.followup.send(f"Queued: **{title}**")
            else:
                await self._play_song_data(song, interaction)

    @discord.app_commands.command(name="play", description="Play a song immediately or enqueue it")
    async def play(self, interaction: discord.Interaction, song: str):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
        await self._resolve_and_play(song, interaction)

    @discord.app_commands.command(name="url", description="Play a YouTube or direct audio stream URL")
    async def url_cmd(self, interaction: discord.Interaction, url: str):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
        if not (url.startswith("http://") or url.startswith("https://")):
            return await interaction.followup.send("Please provide a valid URL starting with http:// or https://")
        await self._resolve_and_play(url, interaction)

    @discord.app_commands.command(name="search", description="Search for songs on the active provider")
    async def search(self, interaction: discord.Interaction, query: str):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
        
        provider = self.state.get_provider(interaction.guild.id)
        fallback_msg = ""
        if provider == "youtube":
            results = await search_songs(query, "youtube", limit=10)
        elif provider == "soundcloud":
            results = await search_songs(query, "soundcloud", limit=10)
        elif provider == "local":
            results = await search_songs(query, "local", limit=10)
        elif provider == "spotify":
            results = await search_songs(query, "youtube", limit=10)
            fallback_msg = " (Spotify fallback)"
        else:
            results = await search_songs(query, "jiosaavn", limit=10)
            
        if not results:
            return await interaction.followup.send(f"No results found for '{query}' on {provider.capitalize()}{fallback_msg}")
            
        await interaction.followup.send(
            embed=self._build_results_embed(query, results),
            view=self.SearchResultsView(self, interaction, results),
        )

    @discord.app_commands.command(name="provider", description="Set the default music provider (YouTube / JioSaavn / SoundCloud / Spotify / Local Files)")
    async def provider_cmd(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)
        await self._ensure_deferred(interaction)
        
        current = self.state.get_provider(interaction.guild.id)
        view = ProviderSelectionView(self.state, interaction.guild.id)
        await interaction.followup.send(
            content=f"Select your default music provider (Current: **{current.capitalize()}**):",
            view=view
        )

    @discord.app_commands.command(name="queue", description="Show the music queue with pagination")
    async def queue_cmd(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)
        await self._ensure_deferred(interaction)
        view = QueueView(self, interaction.guild.id, interaction)
        await interaction.followup.send(embed=view._build_embed(), view=view)

    @discord.app_commands.command(name="lyrics", description="Look up lyrics for the current or a searched song")
    async def lyrics_cmd(self, interaction: discord.Interaction, song: Optional[str] = None):
        await self._ensure_deferred(interaction)
        
        target_song = song
        perma_url = None
        title = None
        
        if not target_song:
            if not interaction.guild:
                return await interaction.followup.send("Server only.")
            session = self._session(interaction.guild.id)
            if not session.current_song_data:
                return await interaction.followup.send("Nothing is playing. Provide a song name to search.")
            
            if session.current_song_data["type"] == "song":
                perma_url = session.current_song_data["song"].get("perma_url")
                title = session.current_song_data["title"]
            else:
                target_song = session.current_song_data["query"]
                title = session.current_song_data["title"]
                
        if target_song:
            results = await JioSaavnHandler.engine.search(target_song)
            if not results:
                return await interaction.followup.send(f"No results found for **{target_song}**")
            best_match = results[0]
            perma_url = best_match.get("perma_url")
            title = JioSaavnHandler.engine.format_string(best_match.get("song") or best_match.get("title") or "Unknown")
            
        if not perma_url:
            return await interaction.followup.send("Could not retrieve details for this song.")
            
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(perma_url, headers=headers)
                if r.status_code != 200:
                    return await interaction.followup.send("Failed to load JioSaavn page.")
                html = r.text
                
            regex = r'"lyrics"\s*:\s*\{\s*"content"\s*:\s*"([\s\S]+?)"\s*,\s*"copyright"'
            match = re.search(regex, html)
            if not match or not match.group(1):
                return await interaction.followup.send(f"No lyrics available on JioSaavn for **{title}**.")
                
            raw_lyrics = match.group(1)
            lyrics = json.loads(f'"{raw_lyrics}"')
            if not lyrics.strip():
                return await interaction.followup.send(f"No lyrics available on JioSaavn for **{title}**.")
                
            pages = []
            lines = lyrics.split("\n")
            current_page = []
            current_length = 0
            for line in lines:
                if current_length + len(line) + 1 > 1500:
                    pages.append("\n".join(current_page))
                    current_page = [line]
                    current_length = len(line)
                else:
                    current_page.append(line)
                    current_length += len(line) + 1
            if current_page:
                pages.append("\n".join(current_page))
                
            view = LyricsView(title, pages, interaction)
            await interaction.followup.send(embed=view._build_embed(), view=view)
            
        except Exception as e:
            logger.error(f"Error fetching lyrics: {e}")
            await interaction.followup.send("An error occurred while fetching lyrics.")

    @discord.app_commands.command(name="bassboost", description="Toggle bass boost filter")
    async def bassboost_cmd(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)
        await self._ensure_deferred(interaction, ephemeral=True)
        guild_id = interaction.guild.id
        enabled = self.state.toggle_effect(guild_id, "bassboost")
        status = "enabled" if enabled else "disabled"
        await interaction.followup.send(f"Bass boost has been {status}.", ephemeral=True)
        await self.reload_current_track(guild_id, interaction)

    @discord.app_commands.command(name="nightcore", description="Toggle nightcore filter (pitch up & speed up)")
    async def nightcore_cmd(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)
        await self._ensure_deferred(interaction, ephemeral=True)
        guild_id = interaction.guild.id
        enabled = self.state.toggle_effect(guild_id, "nightcore")
        status = "enabled" if enabled else "disabled"
        await interaction.followup.send(f"Nightcore filter has been {status}.", ephemeral=True)
        await self.reload_current_track(guild_id, interaction)

    @discord.app_commands.command(name="voicecheck", description="Check voice connection status")
    async def voicecheck(self, interaction: discord.Interaction):
        await self._ensure_deferred(interaction, ephemeral=True)
        lines = self._voice_status_lines(interaction)
        embed = discord.Embed(title="Voice Check", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            return await interaction.response.send_message("Join a voice channel first!", ephemeral=True)
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.response.send_message(
                "This bot is already active in another server. Leave that server's voice channel first.",
                ephemeral=True,
            )
        await self._ensure_deferred(interaction, ephemeral=True)
        channel = interaction.user.voice.channel
        async with self.voice_lock:
            guild_id = interaction.guild.id
            cooldown_remaining = self._voice_join_cooldown_remaining(guild_id)
            if cooldown_remaining > 0:
                message = f"Voice join is cooling down after a failed handshake. Try again in {int(cooldown_remaining) + 1}s."
                return await interaction.followup.send(message, ephemeral=True)

            guild_vc = interaction.guild.voice_client

            if guild_vc and guild_vc.is_connected() and guild_vc.channel and guild_vc.channel.id == channel.id:
                self.state.set_active(guild_vc, channel.id, interaction.channel_id, guild_id)
                message = f"Connected to **{channel.name}**"
                return await interaction.followup.send(message, ephemeral=True)
            if guild_vc and guild_vc.is_connected():
                try:
                    await guild_vc.disconnect(force=True)
                except Exception as e:
                    logger.warning(f"Failed to disconnect existing voice client cleanly: {e}")

            try:
                vc = await channel.connect(self_deaf=True, timeout=20.0, reconnect=False)
                self.state.set_active(vc, channel.id, interaction.channel_id, guild_id)
                self.bot.loop.create_task(self._play_startup_sound(guild_id))
                message = f"Connected to **{channel.name}**"
                if interaction.response.is_done():
                    return await interaction.followup.send(message, ephemeral=True)
                return await interaction.response.send_message(message, ephemeral=True)
            except discord.ConnectionClosed as e:
                code = getattr(e, "code", None)
                self.state.clear(guild_id)
                if code == 4017:
                    self._set_voice_join_cooldown(guild_id, 60.0)
                    logger.error(
                        "Voice join rejected by Discord with 4017 for guild %s channel %s.",
                        guild_id,
                        channel.id,
                    )
                    message = (
                        "Could not join voice right now. Discord rejected the voice handshake (4017). "
                        "Wait a few seconds and try again."
                    )
                    return await interaction.followup.send(message, ephemeral=True)
                logger.error(f"Voice websocket closed during join, code={code}")
                message = f"Could not join voice right now. Discord closed the voice websocket with code {code}."
                return await interaction.followup.send(message, ephemeral=True)
            except asyncio.TimeoutError:
                self.state.clear(guild_id)
                logger.warning(f"Voice connection timed out while joining guild {guild_id} channel {channel.id}")
                message = "Could not join voice right now. The connection timed out."
                return await interaction.followup.send(message, ephemeral=True)
            except Exception as e:
                self.state.clear(guild_id)
                logger.error(f"Voice connection error while joining guild {guild_id} channel {channel.id}: {e}")
                message = "Could not join voice right now. An unexpected voice connection error occurred."
                return await interaction.followup.send(message, ephemeral=True)

    # ==========================================
    # TV (IPTV) Stream Commands
    # ==========================================
    tv_group = discord.app_commands.Group(name="tv", description="IPTV live television streaming commands")

    @tv_group.command(name="browse", description="Browse live TV feeds by country-first selection")
    async def tv_browse(self, interaction: discord.Interaction):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
            
        view = CountrySelectView(interaction.user.id)
        embed = discord.Embed(
            title="📺 IPTV Country Selection",
            description="Select a country from the dropdown below to view its live television feeds.",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, view=view)

    @tv_group.command(name="play", description="Play a specific live TV channel by name")
    async def tv_play(self, interaction: discord.Interaction, channel: str, country: Optional[str] = None):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
            
        if country:
            channels = await IPTVManager.get_channels_by_country(country)
        else:
            channels = await IPTVManager.get_channels()
            
        query_norm = channel.lower()
        matches = [ch for ch in channels if query_norm in ch["name"].lower()]
        
        if not matches:
            country_str = f" in {country}" if country else ""
            return await interaction.followup.send(f"No channels matching '{channel}' found{country_str}.")
            
        channel_data = matches[0]
        selected_url = channel_data["url"]
        
        session = self._session(interaction.guild.id)
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send("You must be in a voice channel to play TV streams.", ephemeral=True)
            
        if not session.voice_client:
            voice_client = await interaction.user.voice.channel.connect()
            self.state.set_active(voice_client, interaction.user.voice.channel.id, interaction.channel.id, interaction.guild.id)
            
        title = f"📺 TV: {channel_data['name']}"
        if channel_data.get("geo_blocked"):
            title += " [Geo-blocked?]"
            
        is_playing = session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused())
        
        if is_playing:
            queue = self.state.queue_for(interaction.guild.id)
            queue.append(QueueItem(query=selected_url, title=title, requested_by=interaction.user.id))
            await interaction.followup.send(f"Queued TV Stream: **{channel_data['name']}**")
        else:
            await self._play_audio(selected_url, interaction, title=title)
            await interaction.followup.send(f"Now streaming TV: **{channel_data['name']}**")

    @tv_group.command(name="search", description="Search TV channels and select one from list")
    async def tv_search(self, interaction: discord.Interaction, query: str, country: Optional[str] = None):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
            
        if country:
            channels = await IPTVManager.get_channels_by_country(country)
        else:
            channels = await IPTVManager.get_channels()
            
        query_norm = query.lower()
        matches = [ch for ch in channels if query_norm in ch["name"].lower()]
        
        if not matches:
            country_str = f" in {country}" if country else ""
            return await interaction.followup.send(f"No channels matching '{query}' found{country_str}.")
            
        view = IPTVChannelView(country or "Search Results", matches, interaction.user.id)
        embed = view.get_embed()
        await interaction.followup.send(embed=embed, view=view)


    # ==========================================
    # Radio Commands
    # ==========================================
    radio_group = discord.app_commands.Group(name="radio", description="Internet radio streaming commands")

    @radio_group.command(name="list", description="List pre-configured radio stations")
    async def radio_list(self, interaction: discord.Interaction):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
            
        presets_path = "bot/radio_stations.json"
        if not os.path.exists(presets_path):
            return await interaction.followup.send("No radio station presets found.")
            
        try:
            with open(presets_path, "r", encoding="utf-8") as f:
                presets = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read radio presets: {e}")
            return await interaction.followup.send("Error loading radio stations.")
            
        view = RadioSelectView(presets, interaction.user.id)
        embed = discord.Embed(
            title="📻 Internet Radio Stations",
            description="Select a pre-configured radio station from the dropdown menu to listen in your voice channel.",
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, view=view)

    @radio_group.command(name="play", description="Play a custom radio stream URL")
    async def radio_play(self, interaction: discord.Interaction, url: str, name: Optional[str] = None):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
            
        if not (url.startswith("http://") or url.startswith("https://")):
            return await interaction.followup.send("Please provide a valid URL starting with http:// or https://")
            
        display_name = name or url.split("/")[-1] or "Custom Stream"
        title = f"📻 Radio: {display_name}"
        
        session = self._session(interaction.guild.id)
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send("You must be in a voice channel to play radio.", ephemeral=True)
            
        if not session.voice_client:
            voice_client = await interaction.user.voice.channel.connect()
            self.state.set_active(voice_client, interaction.user.voice.channel.id, interaction.channel.id, interaction.guild.id)
            
        is_playing = session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused())
        
        if is_playing:
            queue = self.state.queue_for(interaction.guild.id)
            queue.append(QueueItem(query=url, title=title, requested_by=interaction.user.id))
            await interaction.followup.send(f"Queued Radio Stream: **{display_name}**")
        else:
            await self._play_audio(url, interaction, title=title)
            await interaction.followup.send(f"Now playing Radio Stream: **{display_name}**")


    # ==========================================
    # Local Files Playback Commands
    # ==========================================
    local_group = discord.app_commands.Group(name="local", description="Local file playback commands")

    @local_group.command(name="list", description="List available local files in the audio directory")
    async def local_list(self, interaction: discord.Interaction):
        await self._ensure_deferred(interaction)
        files = LocalFileHandler.list_files()
        if not files:
            return await interaction.followup.send("No local audio files found in the `audio/` directory.")
            
        lines = []
        for idx, f in enumerate(files, 1):
            lines.append(f"{idx}. `{f}`")
            
        pages = []
        current_page = []
        char_count = 0
        for line in lines:
            if char_count + len(line) > 1800:
                pages.append("\n".join(current_page))
                current_page = []
                char_count = 0
            current_page.append(line)
            char_count += len(line)
        if current_page:
            pages.append("\n".join(current_page))
            
        embed = discord.Embed(
            title="📂 Local Audio Files",
            description=f"Found **{len(files)}** files. Use `/local play` to play them.\n\n" + pages[0],
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)
        for page in pages[1:]:
            page_embed = discord.Embed(description=page, color=discord.Color.green())
            await interaction.followup.send(embed=page_embed)

    @local_group.command(name="play", description="Play a local audio file")
    async def local_play(self, interaction: discord.Interaction, file: str):
        await self._ensure_deferred(interaction)
        if not self._should_respond(interaction):
            return await interaction.followup.send("Join a voice channel first!")
        if interaction.guild and self.state.is_active_in_other_guild(interaction.guild.id):
            return await interaction.followup.send(
                "This bot is already active in another server. Leave that server's voice channel first."
            )
            
        abs_path = LocalFileHandler.get_absolute_path(file)
        if not abs_path:
            return await interaction.followup.send(f"File `{file}` not found or path is unsafe.")
            
        session = self._session(interaction.guild.id)
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send("You must be in a voice channel to play local files.", ephemeral=True)
            
        if not session.voice_client:
            voice_client = await interaction.user.voice.channel.connect()
            self.state.set_active(voice_client, interaction.user.voice.channel.id, interaction.channel.id, interaction.guild.id)
            
        title = f"📁 Local: {os.path.basename(abs_path)}"
        is_playing = session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused())
        
        if is_playing:
            queue = self.state.queue_for(interaction.guild.id)
            queue.append(QueueItem(query=abs_path, title=title, requested_by=interaction.user.id))
            await interaction.followup.send(f"Queued Local File: **{os.path.basename(abs_path)}**")
        else:
            await self._play_audio(abs_path, interaction, title=title)
            await interaction.followup.send(f"Now playing local file: **{os.path.basename(abs_path)}**")

    @local_play.autocomplete("file")
    async def local_play_autocomplete(self, interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice[str]]:
        files = LocalFileHandler.list_files()
        choices = []
        for f in files:
            if current.lower() in f.lower():
                if len(choices) < 25:
                    choices.append(discord.app_commands.Choice(name=f, value=f))
                else:
                    break
        return choices

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.id == self.bot.user.id:
            if after.channel is None:
                logger.info(f"Bot was disconnected from voice channel in guild {member.guild.id}")
                self.state.clear(member.guild.id)
                self.state.clear_queue(member.guild.id)
            else:
                logger.info(f"Bot was moved to channel {after.channel.name} in guild {member.guild.id}")
                session = self._session(member.guild.id)
                session.voice_channel_id = after.channel.id
            return

        if member.bot:
            return
        voice_client = member.guild.voice_client
        if not voice_client or not voice_client.is_connected() or not voice_client.channel:
            return
        await self._cleanup_if_empty(member.guild)
