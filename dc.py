import discord
from discord.ext import commands
import asyncio
import os
import logging
import sys
from typing import Optional, List
from dotenv import load_dotenv
import yt_dlp
import re

# Load environment variables
load_dotenv()

# ============================================================================
# LOGGING CONFIGURATION - UTF-8 COMPATIBLE
# ============================================================================

# Force UTF-8 for Windows
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('music_bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
BOT_SYNC_GUILD_ID = os.getenv('BOT_SYNC_GUILD_ID')

if not DISCORD_TOKEN:
    raise ValueError('Missing DISCORD_TOKEN in .env file')

# ============================================================================
# YOUTUBE HANDLER
# ============================================================================

class YouTubeHandler:
    """Handle YouTube streaming using yt-dlp"""
    YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'default_search': 'ytsearch',
    'quiet': True,
    'no_warnings': True,
    'socket_timeout': 30,
    'extract_flat': False,
    'geo_bypass': True,
    'cookies': 'cookies.txt',
}

    
    @staticmethod
    def get_ffmpeg_options(volume: float = 1.0):
        """Get FFmpeg options with volume control"""
        return {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': f'-vn -filter:a "volume={volume}"'
        }
    
    @staticmethod
    def is_youtube_url(query: str) -> bool:
        """Check if query is a YouTube URL"""
        youtube_patterns = [
            r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/',
            r'youtube\.com/watch\?v=',
            r'youtu\.be/'
        ]
        return any(re.search(pattern, query) for pattern in youtube_patterns)
    
    @classmethod
    async def search_youtube(cls, query: str, max_results: int = 5) -> Optional[List[dict]]:
        """Search YouTube and return multiple results"""
        try:
            loop = asyncio.get_event_loop()
            
            def _search():
                with yt_dlp.YoutubeDL(cls.YDL_OPTIONS) as ydl:
                    logger.info(f"Searching YouTube for: {query}")
                    info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
                    
                    if not info or 'entries' not in info or not info['entries']:
                        return None
                    
                    results = []
                    for entry in info['entries'][:max_results]:
                        duration_seconds = entry.get('duration', 0)
                        minutes, seconds = divmod(duration_seconds, 60)
                        duration_str = f"{minutes}:{seconds:02d}"
                        
                        results.append({
                            'title': entry.get('title', 'Unknown'),
                            'url': entry.get('webpage_url', ''),
                            'duration': duration_str,
                            'uploader': entry.get('uploader', 'Unknown')
                        })
                    
                    return results
            
            return await loop.run_in_executor(None, _search)
            
        except Exception as e:
            logger.error(f"Error searching YouTube: {e}")
            return None
    
    @classmethod
    async def get_audio_source(cls, query: str, volume: float = 1.0) -> Optional[tuple]:
        """Get audio source from YouTube"""
        try:
            loop = asyncio.get_event_loop()
            
            def _extract():
                with yt_dlp.YoutubeDL(cls.YDL_OPTIONS) as ydl:
                    if cls.is_youtube_url(query):
                        logger.info(f"Extracting from URL: {query}")
                        info = ydl.extract_info(query, download=False)
                    else:
                        logger.info(f"Searching YouTube for: {query}")
                        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                        if info and 'entries' in info and info['entries']:
                            info = info['entries'][0]
                        else:
                            return None, None
                    
                    if not info:
                        return None, None
                    
                    stream_url = info.get('url')
                    title = info.get('title', 'Unknown')
                    
                    if not stream_url:
                        return None, None
                    
                    return stream_url, title
            
            stream_url, title = await loop.run_in_executor(None, _extract)
            
            if not stream_url:
                return None, None
            
            audio_source = discord.FFmpegPCMAudio(stream_url, **cls.get_ffmpeg_options(volume))
            logger.info(f"Audio source created: {title}")
            return audio_source, title
            
        except Exception as e:
            logger.error(f"Error getting audio source: {e}")
            return None, None

# ============================================================================
# BOT STATE MANAGER
# ============================================================================

class BotState:
    """Manages single active voice channel state with linked text channel"""
    
    def __init__(self):
        self.voice_client: Optional[discord.VoiceClient] = None
        self.voice_channel_id: Optional[int] = None
        self.linked_text_channel_id: Optional[int] = None
        self.current_song_title: Optional[str] = None
        self.is_playing = False
        self.volume: float = 1.0
    
    def set_active(self, voice_client: discord.VoiceClient, voice_channel_id: int, text_channel_id: int):
        """Set the active voice client and link the text channel"""
        self.voice_client = voice_client
        self.voice_channel_id = voice_channel_id
        self.linked_text_channel_id = text_channel_id
        logger.info(f"Bot active - Voice: {voice_channel_id}, Text: {text_channel_id}")
    
    def clear(self):
        """Clear all state"""
        self.voice_client = None
        self.voice_channel_id = None
        self.linked_text_channel_id = None
        self.current_song_title = None
        self.is_playing = False
        logger.info("Bot state cleared")
    
    def is_bot_active(self) -> bool:
        """Check if bot is currently in a voice channel"""
        return self.voice_client is not None and self.voice_channel_id is not None

# ============================================================================
# MUSIC BOT COG
# ============================================================================

class MusicBot(commands.Cog):
    """Strict voice-channel-locked music bot"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = BotState()
        self.search_results_cache = {}
    
    def _get_user_voice_channel_id(self, interaction: discord.Interaction) -> Optional[int]:
        """Get user's voice channel ID"""
        if not interaction.user.voice or not interaction.user.voice.channel:
            return None
        return interaction.user.voice.channel.id
    
    def _should_respond(self, interaction: discord.Interaction, is_join_command: bool = False) -> bool:
        """
        STRICT AUTHORIZATION LOGIC
        
        Scenario 1: Bot is NOT in any voice channel (state.is_bot_active() == False)
            - User MUST be in a voice channel
            - Text channel restriction: NONE (accept from any text channel)
            - Only for /join command
        
        Scenario 2: Bot IS in a voice channel (state.is_bot_active() == True)
            For /join command:
                - User MUST be in a voice channel (can be different from bot)
                - Text channel: Accept from any text channel
            For other commands:
                - User MUST be in SAME voice channel as bot
                - Text channel MUST be the linked text channel
        
        Returns: True to respond, False to silently ignore
        """
        user_voice_id = self._get_user_voice_channel_id(interaction)
        text_channel_id = interaction.channel_id
        
        # SCENARIO 1: Bot not active, only /join allowed
        if not self.state.is_bot_active():
            if not is_join_command:
                # Bot not active, non-join command → silent ignore
                logger.debug(f"❌ Bot not active, ignoring non-join command from {interaction.user}")
                return False
            
            # Bot not active, /join command → user must be in voice
            if not user_voice_id:
                logger.debug(f"❌ Bot not active, user not in voice: {interaction.user}")
                return False
            
            logger.debug(f"✅ Bot not active, allowing /join from user in voice {user_voice_id}")
            return True
        
        # SCENARIO 2: Bot is active
        if is_join_command:
            # Bot active, /join command → user must be in voice (any voice channel)
            if not user_voice_id:
                logger.debug(f"❌ Bot active, user not in voice: {interaction.user}")
                return False
            
            logger.debug(f"✅ Bot active, allowing /join from user in voice {user_voice_id}")
            return True
        
        # Bot active, other commands → strict checks
        
        # Check 1: User must be in SAME voice channel as bot
        if user_voice_id != self.state.voice_channel_id:
            logger.debug(f"❌ User in wrong voice: {user_voice_id} (bot in {self.state.voice_channel_id})")
            return False
        
        # Check 2: Text channel must be the linked one
        if text_channel_id != self.state.linked_text_channel_id:
            logger.debug(f"❌ Wrong text channel: {text_channel_id} (linked: {self.state.linked_text_channel_id})")
            return False
        
        logger.debug(f"✅ Authorized: User in correct voice & text channel")
        return True
    
    async def _play_audio(self, query: str, interaction: discord.Interaction):
        """Play audio"""
        if self.state.voice_client.is_playing():
            self.state.voice_client.stop()
        
        audio_source, title = await YouTubeHandler.get_audio_source(query, self.state.volume)
        
        if not audio_source or not title:
            await interaction.followup.send(f"Could not find: {query}")
            return
        
        def after_playback(error):
            if error:
                logger.error(f"Playback error: {error}")
            self.state.is_playing = False
            self.state.current_song_title = None
        
        self.state.voice_client.play(audio_source, after=after_playback)
        self.state.is_playing = True
        self.state.current_song_title = title
        
        await interaction.followup.send(f"▶️ Now playing: **{title}**")
        logger.info(f"Playing: {title}")
    
    # ========================================================================
    # EVENT: AUTO-LEAVE WHEN ALONE
    # ========================================================================
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Auto-leave when bot is alone in voice channel"""
        try:
            if not before.channel:
                return
            
            if not self.state.is_bot_active() or self.state.voice_channel_id != before.channel.id:
                return
            
            members_in_channel = [m for m in before.channel.members if not m.bot]
            
            if len(members_in_channel) == 0:
                logger.info(f"Bot alone in {before.channel.name}, leaving...")
                
                # Send goodbye to linked text channel
                if self.state.linked_text_channel_id:
                    try:
                        channel = self.bot.get_channel(self.state.linked_text_channel_id)
                        if channel:
                            await channel.send("Everyone left. Goodbye! 👋")
                    except:
                        pass
                
                await self.state.voice_client.disconnect()
                self.state.clear()
                
        except Exception as e:
            logger.error(f"Error in voice state update: {e}")
    
    # ========================================================================
    # SLASH COMMANDS
    # ========================================================================
    
    @discord.app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        """Join user's voice channel and link this text channel"""
        try:
            if not self._should_respond(interaction, is_join_command=True):
                return
            
            user_voice_channel = interaction.user.voice.channel
            
            # Check permissions
            perms = user_voice_channel.permissions_for(interaction.guild.me)
            if not perms.connect or not perms.speak:
                logger.warning(f"Missing permissions in {user_voice_channel.name}")
                return
            
            # If bot is already connected, disconnect first
            if self.state.is_bot_active():
                old_channel = self.state.voice_client.channel.name
                await self.state.voice_client.disconnect()
                logger.info(f"Left {old_channel} to join {user_voice_channel.name}")
            
            # Connect to user's voice channel
            voice_client = await user_voice_channel.connect()
            self.state.set_active(voice_client, user_voice_channel.id, interaction.channel_id)
            
            await interaction.response.send_message(
                f"✅ Joined: **{user_voice_channel.name}**\n"
                f"📝 Linked to this text channel\n"
                f"🔊 Volume: {int(self.state.volume * 100)}%"
            )
            
        except Exception as e:
            logger.error(f"Error in join: {e}")
    
    @discord.app_commands.command(name="search", description="Search YouTube")
    @discord.app_commands.describe(query="Search query")
    async def search(self, interaction: discord.Interaction, query: str):
        """Search YouTube"""
        try:
            if not self._should_respond(interaction):
                return
            
            await interaction.response.defer()
            
            results = await YouTubeHandler.search_youtube(query, max_results=5)
            
            if not results:
                await interaction.followup.send(f"No results for: {query}")
                return
            
            self.search_results_cache[interaction.user.id] = results
            
            result_text = "**🔍 Search Results:**\n\n"
            for i, result in enumerate(results, 1):
                result_text += f"{i}. **{result['title']}**\n"
                result_text += f"   ⏱️ {result['duration']} | 👤 {result['uploader']}\n\n"
            
            result_text += f"Use `/pick <number>` to play (1-{len(results)})"
            
            await interaction.followup.send(result_text)
            
        except Exception as e:
            logger.error(f"Error in search: {e}")
    
    @discord.app_commands.command(name="pick", description="Pick search result")
    @discord.app_commands.describe(number="Result number (1-5)")
    async def pick(self, interaction: discord.Interaction, number: int):
        """Pick a search result to play"""
        try:
            if not self._should_respond(interaction):
                return
            
            if interaction.user.id not in self.search_results_cache:
                await interaction.response.send_message("Use /search first")
                return
            
            results = self.search_results_cache[interaction.user.id]
            
            if not 1 <= number <= len(results):
                await interaction.response.send_message(f"Pick 1-{len(results)}")
                return
            
            await interaction.response.defer()
            
            selected = results[number - 1]
            await self._play_audio(selected['url'], interaction)
            
            del self.search_results_cache[interaction.user.id]
            
        except Exception as e:
            logger.error(f"Error in pick: {e}")
    
    @discord.app_commands.command(name="play", description="Play song or YouTube link")
    @discord.app_commands.describe(song="Song name or YouTube link")
    async def play(self, interaction: discord.Interaction, song: str):
        """Play a song"""
        try:
            if not self._should_respond(interaction):
                return
            
            await interaction.response.defer()
            await self._play_audio(song, interaction)
            
        except Exception as e:
            logger.error(f"Error in play: {e}")
    
    @discord.app_commands.command(name="volume", description="Set volume (0-200)")
    @discord.app_commands.describe(level="Volume level (0-200)")
    async def volume(self, interaction: discord.Interaction, level: int):
        """Set volume"""
        try:
            if not self._should_respond(interaction):
                return
            
            if not 0 <= level <= 200:
                await interaction.response.send_message("Volume must be 0-200")
                return
            
            self.state.volume = level / 100.0
            await interaction.response.send_message(
                f"🔊 Volume set to {level}%\n(Applies to next song)"
            )
            
        except Exception as e:
            logger.error(f"Error in volume: {e}")
    
    @discord.app_commands.command(name="pause", description="Pause song")
    async def pause(self, interaction: discord.Interaction):
        """Pause"""
        try:
            if not self._should_respond(interaction):
                return
            
            if not self.state.voice_client or not self.state.voice_client.is_playing():
                return
            
            self.state.voice_client.pause()
            await interaction.response.send_message(f"⏸️ Paused: {self.state.current_song_title}")
            
        except Exception as e:
            logger.error(f"Error in pause: {e}")
    
    @discord.app_commands.command(name="resume", description="Resume song")
    async def resume(self, interaction: discord.Interaction):
        """Resume"""
        try:
            if not self._should_respond(interaction):
                return
            
            if not self.state.voice_client or not self.state.voice_client.is_paused():
                return
            
            self.state.voice_client.resume()
            await interaction.response.send_message(f"▶️ Resumed: {self.state.current_song_title}")
            
        except Exception as e:
            logger.error(f"Error in resume: {e}")
    
    @discord.app_commands.command(name="stop", description="Stop song")
    async def stop(self, interaction: discord.Interaction):
        """Stop"""
        try:
            if not self._should_respond(interaction):
                return
            
            if not self.state.voice_client or not self.state.voice_client.is_playing():
                return
            
            self.state.voice_client.stop()
            self.state.is_playing = False
            self.state.current_song_title = None
            
            await interaction.response.send_message("⏹️ Stopped")
            
        except Exception as e:
            logger.error(f"Error in stop: {e}")
    
    @discord.app_commands.command(name="go", description="Leave voice channel")
    async def go(self, interaction: discord.Interaction):
        """Leave voice channel"""
        try:
            if not self._should_respond(interaction):
                return
            
            if not self.state.is_bot_active():
                return
            
            channel_name = self.state.voice_client.channel.name
            await self.state.voice_client.disconnect()
            self.state.clear()
            
            await interaction.response.send_message(f"👋 Left: {channel_name}")
            
        except Exception as e:
            logger.error(f"Error in go: {e}")
    
    @discord.app_commands.command(name="nowplaying", description="Current song")
    async def nowplaying(self, interaction: discord.Interaction):
        """Show current song"""
        try:
            if not self._should_respond(interaction):
                return
            
            if not self.state.is_playing or not self.state.current_song_title:
                return
            
            status = "⏸️ Paused" if self.state.voice_client.is_paused() else "▶️ Playing"
            await interaction.response.send_message(
                f"{status}: **{self.state.current_song_title}**\n"
                f"🔊 Volume: {int(self.state.volume * 100)}%"
            )
            
        except Exception as e:
            logger.error(f"Error in nowplaying: {e}")

# ============================================================================
# BOT CLASS
# ============================================================================

class Bot(commands.Bot):
    """Custom Bot class"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = False
        intents.voice_states = True
        
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        """Load cogs and sync commands"""
        await self.add_cog(MusicBot(self))
        logger.info("MusicBot cog loaded")
        
        if BOT_SYNC_GUILD_ID:
            guild = discord.Object(id=int(BOT_SYNC_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Synced to guild {BOT_SYNC_GUILD_ID}")
        else:
            await self.tree.sync()
            logger.info("Synced globally")
    
    async def on_ready(self):
        """Bot ready"""
        logger.info(f"Logged in as {self.user}")
        logger.info(f"Connected to {len(self.guilds)} guilds")
        logger.info("✅ Bot ready!")

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point"""
    bot = Bot()
    
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")

if __name__ == "__main__":
    main()
