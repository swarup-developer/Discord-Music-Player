import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List, Any, Optional
import discord
import httpx

logger = logging.getLogger(__name__)

IPTV_URL = "https://iptv-org.github.io/iptv/index.language.m3u"
IPTV_CAT_URL = "https://iptv-org.github.io/iptv/index.m3u"
CACHE_FILE = "iptv_lang_cache.json"
CACHE_EXPIRY = 24 * 60 * 60  # 24 hours in seconds

# Mapping of common 2-letter country codes to human-readable names
COUNTRY_CODES = {
    "in": "India",
    "us": "United States",
    "gb": "United Kingdom",
    "uk": "United Kingdom",
    "de": "Germany",
    "fr": "France",
    "es": "Spain",
    "it": "Italy",
    "ca": "Canada",
    "au": "Australia",
    "ru": "Russia",
    "br": "Brazil",
    "tr": "Turkey",
    "id": "Indonesia",
    "pk": "Pakistan",
    "bd": "Bangladesh",
    "ua": "Ukraine",
    "cn": "China",
    "jp": "Japan",
    "kr": "South Korea",
    "mx": "Mexico",
    "ar": "Argentina",
    "cl": "Chile",
    "co": "Colombia",
    "pe": "Peru",
    "pl": "Poland",
    "nl": "Netherlands",
    "se": "Sweden",
    "no": "Norway",
    "fi": "Finland",
    "dk": "Denmark",
    "za": "South Africa",
    "nz": "New Zealand",
    "ae": "United Arab Emirates",
    "sa": "Saudi Arabia",
    "sg": "Singapore",
    "my": "Malaysia",
    "th": "Thailand",
    "ph": "Philippines",
    "vn": "Vietnam",
    "eg": "Egypt",
    "ir": "Iran",
    "ro": "Romania",
    "hu": "Hungary",
    "cz": "Czech Republic",
    "pt": "Portugal",
    "gr": "Greece",
    "be": "Belgium",
    "ch": "Switzerland",
    "at": "Austria",
    "ie": "Ireland",
    "il": "Israel"
}

# Reverse mapping for easy lookup
COUNTRY_NAME_TO_CODE = {v.lower(): k for k, v in COUNTRY_CODES.items()}

class IPTVManager:
    _channels: List[Dict[str, Any]] = []
    _countries: List[str] = []
    _channels_by_country: Dict[str, List[Dict[str, Any]]] = {}
    _channels_by_language: Dict[str, List[Dict[str, Any]]] = {}
    _is_loading: bool = False

    @classmethod
    async def get_channels(cls) -> List[Dict[str, Any]]:
        if not cls._channels:
            await cls.load_channels()
        return cls._channels

    @classmethod
    async def get_channels_by_country(cls, country: str) -> List[Dict[str, Any]]:
        if not cls._channels:
            await cls.load_channels()
        normalized = country.strip().lower()
        
        # Check if country name is in our mapping
        code = COUNTRY_NAME_TO_CODE.get(normalized)
        if not code:
            # Maybe it's already a 2-letter code
            code = normalized if len(normalized) == 2 else None
            
        if code:
            return cls._channels_by_country.get(code, [])
            
        # Fallback search by matching country code in tvg-id or group-title
        results = []
        for ch in cls._channels:
            if ch.get("country_code") == normalized or ch.get("group_title", "").lower() == normalized:
                results.append(ch)
        return results

    @classmethod
    async def get_channels_by_language(cls, language: str) -> List[Dict[str, Any]]:
        if not cls._channels:
            await cls.load_channels()
        normalized = language.strip().lower()
        return [ch for ch in cls._channels if ch.get("language", "").lower() == normalized]

    @classmethod
    async def get_channels_filtered(cls, language: Optional[str] = None, country: Optional[str] = None, category: Optional[str] = None) -> List[Dict[str, Any]]:
        if not cls._channels:
            await cls.load_channels()
        
        results = cls._channels
        if language and language.lower() != "any":
            lang_lower = language.lower()
            results = [ch for ch in results if ch.get("language", "").lower() == lang_lower]
            
        if country and country.lower() != "any":
            country_lower = country.lower()
            code = COUNTRY_NAME_TO_CODE.get(country_lower) or country_lower
            results = [ch for ch in results if ch.get("country_code", "").lower() == code]
            
        if category and category.lower() != "any" and category.lower() != "all categories":
            cat_lower = category.lower()
            results = [ch for ch in results if ch.get("category", "").lower() == cat_lower]
            
        return results

    @classmethod
    def get_all_languages(cls) -> List[str]:
        langs = set()
        for ch in cls._channels:
            lang = ch.get("language")
            if lang:
                langs.add(lang)
        return sorted(list(langs))

    @classmethod
    def get_all_countries(cls) -> List[str]:
        # Return sorted list of human readable countries that have channels
        countries_with_names = []
        for code in cls._channels_by_country.keys():
            name = COUNTRY_CODES.get(code)
            if name:
                countries_with_names.append(name)
            else:
                countries_with_names.append(code.upper())
        return sorted(countries_with_names)

    @classmethod
    async def load_channels(cls, force_refresh: bool = False) -> None:
        if cls._is_loading:
            while cls._is_loading:
                await asyncio.sleep(0.5)
            return

        cls._is_loading = True
        try:
            # Check local cache first
            if not force_refresh and os.path.exists(CACHE_FILE):
                mtime = os.path.getmtime(CACHE_FILE)
                if (time.time() - mtime) < CACHE_EXPIRY:
                    try:
                        with open(CACHE_FILE, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if data:
                                cls._channels = data
                                cls._rebuild_indices()
                                logger.info(f"Loaded {len(cls._channels)} IPTV channels from local cache.")
                                cls._is_loading = False
                                return
                    except Exception as e:
                        logger.error(f"Failed to read IPTV cache: {e}")

            # Fetch and parse indexes
            logger.info("Fetching IPTV indexes from iptv-org...")
            async with httpx.AsyncClient(timeout=30.0) as client:
                response_lang = await client.get(IPTV_URL)
                response_cat = await client.get(IPTV_CAT_URL)
                
                if response_lang.status_code == 200:
                    text_lang = response_lang.text
                    cls._channels = cls._parse_m3u(text_lang, is_lang=True)
                    
                    if response_cat.status_code == 200:
                        text_cat = response_cat.text
                        cat_channels = cls._parse_m3u(text_cat, is_lang=False)
                        
                        # Merge categories by URL
                        url_map = {ch["url"]: ch for ch in cls._channels}
                        for ch in cat_channels:
                            if ch["url"] in url_map:
                                url_map[ch["url"]]["category"] = ch.get("category", "General")
                                
                    cls._rebuild_indices()
                    # Write to cache
                    try:
                        with open(CACHE_FILE, "w", encoding="utf-8") as f:
                            json.dump(cls._channels, f, indent=4)
                        logger.info("Saved IPTV channels to local cache.")
                    except Exception as e:
                        logger.error(f"Failed to save IPTV cache: {e}")
                else:
                    logger.error(f"Failed to fetch IPTV index. Status code: {response_lang.status_code}")
        except Exception as e:
            logger.error(f"Error loading IPTV channels: {e}")
        finally:
            cls._is_loading = False

    @classmethod
    def _parse_m3u(cls, content: str, is_lang: bool = True) -> List[Dict[str, Any]]:
        channels = []
        lines = content.splitlines()
        
        current_info = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTM3U"):
                continue
            if line.startswith("#EXTINF:"):
                # Parse metadata
                info = {}
                
                # Extract attributes using regex
                tvg_id_match = re.search(r'tvg-id="([^"]*)"', line)
                tvg_logo_match = re.search(r'tvg-logo="([^"]*)"', line)
                group_title_match = re.search(r'group-title="([^"]*)"', line)
                
                info["tvg_id"] = tvg_id_match.group(1) if tvg_id_match else ""
                info["logo"] = tvg_logo_match.group(1) if tvg_logo_match else ""
                
                if is_lang:
                    info["language"] = group_title_match.group(1) if group_title_match else ""
                    info["category"] = "General"
                else:
                    info["language"] = ""
                    info["category"] = group_title_match.group(1) if group_title_match else "General"
                
                # Extract channel name (everything after the last comma)
                comma_index = line.rfind(",")
                if comma_index != -1:
                    info["name"] = line[comma_index + 1:].strip()
                else:
                    info["name"] = "Unknown Channel"
                    
                # Extract country code from tvg-id (e.g. 9XJalwa.in -> in)
                country_code = ""
                if info["tvg_id"]:
                    base_id = info["tvg_id"].split("@")[0]
                    parts = base_id.split(".")
                    if len(parts) > 1:
                        suffix = parts[-1].lower()
                        if len(suffix) == 2 and suffix.isalpha():
                            country_code = suffix
                info["country_code"] = country_code
                
                # Check for geo-blocking
                info["geo_blocked"] = "[geo-blocked]" in info["name"].lower() or "geo-blocked" in line.lower()
                
                current_info = info
            elif line.startswith("http://") or line.startswith("https://"):
                if current_info:
                    current_info["url"] = line
                    channels.append(current_info)
                    current_info = None
                    
        return channels

    @classmethod
    def _rebuild_indices(cls) -> None:
        cls._channels_by_country = {}
        cls._channels_by_language = {}
        for ch in cls._channels:
            # Country indexing
            code = ch.get("country_code")
            if code:
                if code not in cls._channels_by_country:
                    cls._channels_by_country[code] = []
                cls._channels_by_country[code].append(ch)
            
            # Language indexing
            lang = ch.get("language")
            if lang:
                lang_key = lang.strip().lower()
                if lang_key not in cls._channels_by_language:
                    cls._channels_by_language[lang_key] = []
                cls._channels_by_language[lang_key].append(ch)

# Define Interactive Views for Discord
class CountrySelect(discord.ui.Select):
    def __init__(self, popular_countries: List[str]):
        options = [
            discord.SelectOption(label=country, value=country, emoji="🌐")
            for country in popular_countries
        ]
        super().__init__(
            placeholder="Select a country to browse TV channels...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="iptv_country_select"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_country = self.values[0]
        
        # Get channels for this country
        channels = await IPTVManager.get_channels_by_country(selected_country)
        if not channels:
            await interaction.followup.send(f"No channels found for {selected_country}.", ephemeral=True)
            return

        # Show channels view
        view = IPTVChannelView(selected_country, channels, interaction.user.id)
        embed = view.get_embed()
        await interaction.edit_original_response(embed=embed, view=view)


class CountrySelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180.0)
        self.user_id = user_id
        
        # Popular countries list with India prioritized
        popular = ["India", "United States", "United Kingdom", "Germany", "France", "Spain", "Italy", "Canada", "Australia", "Brazil", "Russia", "Turkey", "Indonesia", "Pakistan", "Bangladesh", "Ukraine", "Japan", "South Korea"]
        self.add_item(CountrySelect(popular))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the user who started the browse command can interact with this menu.", ephemeral=True)
            return False
        return True


class LanguageSelect(discord.ui.Select):
    def __init__(self, popular_languages: List[str]):
        options = [
            discord.SelectOption(label=lang, value=lang, emoji="🗣️")
            for lang in popular_languages
        ]
        super().__init__(
            placeholder="Select a language to browse TV channels...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="iptv_language_select"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_lang = self.values[0]
        
        # Get unique countries for this language
        channels = await IPTVManager.get_channels_filtered(language=selected_lang)
        
        # Extract countries
        country_codes_set = {ch.get("country_code") for ch in channels if ch.get("country_code")}
        
        # Build options list
        options = []
        options.append(discord.SelectOption(label="Any Country", value="any", emoji="🌐"))
        
        for code in sorted(list(country_codes_set)):
            name = COUNTRY_CODES.get(code)
            if name:
                options.append(discord.SelectOption(label=name, value=code, emoji="📍"))
            else:
                options.append(discord.SelectOption(label=code.upper(), value=code, emoji="📍"))
                 
        if len(options) > 25:
            options = options[:25]
             
        view = IPTVCountrySelectView(self.view.user_id, selected_lang, options)
        embed = discord.Embed(
            title=f"📺 IPTV Country Selection ({selected_lang})",
            description=f"Select a country for **{selected_lang}** channels.",
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed, view=view)


class LanguageSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180.0)
        self.user_id = user_id
        
        # Top 25 languages for dropdown selection
        popular = ["English", "Hindi", "Spanish", "French", "German", "Arabic", "Portuguese", "Russian", "Japanese", "Chinese", "Korean", "Italian", "Tamil", "Telugu", "Bengali", "Malayalam", "Kannada", "Turkish", "Vietnamese", "Thai", "Polish", "Dutch", "Urdu", "Punjabi", "Gujarati"]
        self.add_item(LanguageSelect(popular))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the user who started the browse command can interact with this menu.", ephemeral=True)
            return False
        return True


class IPTVCountrySelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(
            placeholder="Select a country...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="iptv_step_country_select"
        )
        
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_country = self.values[0]
        
        # Get channels for this language and country
        channels = await IPTVManager.get_channels_filtered(
            language=self.view.selected_lang,
            country=selected_country
        )
        
        # Extract unique categories
        categories_set = {ch.get("category") for ch in channels if ch.get("category")}
        
        # Build options list
        options = []
        options.append(discord.SelectOption(label="All Categories", value="any", emoji="📁"))
        
        for cat in sorted(list(categories_set)):
            if cat and cat.lower() != "undefined":
                options.append(discord.SelectOption(label=cat, value=cat, emoji="🏷️"))
                
        if len(options) > 25:
            options = options[:25]
            
        view = IPTVCategorySelectView(self.view.user_id, self.view.selected_lang, selected_country, options)
        embed = discord.Embed(
            title=f"📺 IPTV Category Selection ({self.view.selected_lang})",
            description=f"Select a category for channels in **{self.view.selected_lang}**.",
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed, view=view)


class IPTVCountrySelectView(discord.ui.View):
    def __init__(self, user_id: int, selected_lang: str, options: List[discord.SelectOption]):
        super().__init__(timeout=180.0)
        self.user_id = user_id
        self.selected_lang = selected_lang
        self.options = options
        
        self.add_item(IPTVCountrySelect(options))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the user who started the browse command can interact with this menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⬅ Back to Languages", style=discord.ButtonStyle.danger, custom_id="iptv_back_to_langs", row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        view = LanguageSelectView(self.user_id)
        embed = discord.Embed(
            title="📺 IPTV Language Selection",
            description="Select a language from the dropdown below to view its live television feeds.",
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed, view=view)


class IPTVCategorySelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(
            placeholder="Select a category (Optional)...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="iptv_step_category_select"
        )
        
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_category = self.values[0]
        
        # Get final channels list
        channels = await IPTVManager.get_channels_filtered(
            language=self.view.selected_lang,
            country=self.view.selected_country,
            category=selected_category
        )
        
        if not channels:
            await interaction.followup.send("No channels found matching selection.", ephemeral=True)
            return
            
        # Show channels view
        view = IPTVChannelView(
            country=f"{self.view.selected_lang} - {self.view.selected_country.upper()}",
            channels=channels,
            user_id=self.view.user_id,
            is_language=True,
            language=self.view.selected_lang,
            country_code=self.view.selected_country,
            category=selected_category
        )
        embed = view.get_embed()
        await interaction.edit_original_response(embed=embed, view=view)


class IPTVCategorySelectView(discord.ui.View):
    def __init__(self, user_id: int, selected_lang: str, selected_country: str, options: List[discord.SelectOption]):
        super().__init__(timeout=180.0)
        self.user_id = user_id
        self.selected_lang = selected_lang
        self.selected_country = selected_country
        self.options = options
        
        self.add_item(IPTVCategorySelect(options))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the user who started the browse command can interact with this menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⬅ Back to Countries", style=discord.ButtonStyle.danger, custom_id="iptv_back_to_countries", row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # Reconstruct CountrySelect options
        channels = await IPTVManager.get_channels_filtered(language=self.selected_lang)
        country_codes_set = {ch.get("country_code") for ch in channels if ch.get("country_code")}
        options = []
        options.append(discord.SelectOption(label="Any Country", value="any", emoji="🌐"))
        for code in sorted(list(country_codes_set)):
            name = COUNTRY_CODES.get(code)
            if name:
                options.append(discord.SelectOption(label=name, value=code, emoji="📍"))
            else:
                options.append(discord.SelectOption(label=code.upper(), value=code, emoji="📍"))
        if len(options) > 25:
            options = options[:25]
            
        view = IPTVCountrySelectView(self.user_id, self.selected_lang, options)
        embed = discord.Embed(
            title=f"📺 IPTV Country Selection ({self.selected_lang})",
            description=f"Select a country for **{self.selected_lang}** channels.",
            color=discord.Color.blue()
        )
        await interaction.edit_original_response(embed=embed, view=view)


class ChannelSelect(discord.ui.Select):
    def __init__(self, channels: List[Dict[str, Any]], page: int = 0):
        self.channels = channels
        self.page = page
        
        # Display 25 channels starting from page * 25
        start = page * 25
        end = start + 25
        page_channels = channels[start:end]
        
        options = []
        for ch in page_channels:
            name = ch["name"]
            if len(name) > 80:
                name = name[:77] + "..."
            
            geo_emoji = "⚠️" if ch.get("geo_blocked") else "📺"
            category = ch.get("group_title", "General")
            
            options.append(
                discord.SelectOption(
                    label=name,
                    value=ch["url"],
                    description=f"Category: {category}",
                    emoji=geo_emoji
                )
            )
            
        super().__init__(
            placeholder=f"Select a TV channel to play (Page {page + 1})...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="iptv_channel_select"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_url = self.values[0]
        
        # Find chosen channel details
        channel_data = next((ch for ch in self.channels if ch["url"] == selected_url), None)
        if not channel_data:
            return
            
        # Play channel via MusicCog
        music_cog = interaction.client.get_cog("MusicCog")
        if not music_cog:
            await interaction.followup.send("Music functionality is currently unavailable.", ephemeral=True)
            return

        # Set search provider to play this URL directly
        # Format the channel title for cogs_music
        title = f"📺 TV: {channel_data['name']}"
        if channel_data.get("geo_blocked"):
            title += " [Geo-blocked?]"

        # Call the resolver in MusicCog
        await music_cog._ensure_deferred(interaction)
        
        # Connect to voice if not already connected
        session = music_cog._session(interaction.guild.id)
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("You must be in a voice channel to play TV streams.", ephemeral=True)
            return
            
        if not session.voice_client:
            voice_client = await interaction.user.voice.channel.connect()
            music_cog.state.set_active(voice_client, interaction.user.voice.channel.id, interaction.channel.id, interaction.guild.id)
            
        is_playing = session.voice_client and (session.voice_client.is_playing() or session.voice_client.is_paused())
        
        if is_playing:
            # Add to queue
            queue = music_cog.state.queue_for(interaction.guild.id)
            from .state import QueueItem
            queue.append(QueueItem(query=selected_url, title=title, requested_by=interaction.user.id))
            await interaction.followup.send(f"Queued TV Stream: **{channel_data['name']}**")
        else:
            # Play immediately
            await music_cog._play_audio(selected_url, interaction, title=title)
            await interaction.followup.send(f"Now streaming TV: **{channel_data['name']}**")


class IPTVChannelView(discord.ui.View):
    def __init__(self, country: str, channels: List[Dict[str, Any]], user_id: int, page: int = 0, is_language: bool = False, language: str = "", country_code: str = "", category: str = ""):
        super().__init__(timeout=180.0)
        self.country = country
        self.channels = channels
        self.user_id = user_id
        self.page = page
        self.is_language = is_language
        self.language = language
        self.country_code = country_code
        self.category = category
        self.max_page = (len(channels) - 1) // 25
        
        # Update back button label dynamically
        if self.is_language:
            self.back_button.label = "⬅ Back to Categories"
        else:
            self.back_button.label = "⬅ Back to Countries"
        
        # Add Select
        self.select = ChannelSelect(channels, page)
        self.add_item(self.select)
        
        # Update button states
        self.update_buttons()

    def update_buttons(self):
        # Enable/Disable pagination buttons
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.max_page

    def get_embed(self) -> discord.Embed:
        prefix = "Language:" if self.is_language else "Country:"
        if self.country == "Search Results":
            title_text = "📺 Search Results"
        else:
            title_text = f"📺 TV Channels ({prefix} {self.country})"
        embed = discord.Embed(
            title=title_text,
            description=f"Showing channels **{self.page * 25 + 1}** to **{min((self.page + 1) * 25, len(self.channels))}** of **{len(self.channels)}**.",
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Page {self.page + 1} of {self.max_page + 1}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the user who started the browse command can interact with this menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, custom_id="iptv_prev_page", row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.page > 0:
            self.page -= 1
            # Recreate select and items
            self.clear_items()
            self.select = ChannelSelect(self.channels, self.page)
            self.add_item(self.select)
            self.add_item(self.prev_button)
            self.add_item(self.next_button)
            self.add_item(self.back_button)
            self.update_buttons()
            
            embed = self.get_embed()
            await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="iptv_next_page", row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.page < self.max_page:
            self.page += 1
            # Recreate select and items
            self.clear_items()
            self.select = ChannelSelect(self.channels, self.page)
            self.add_item(self.select)
            self.add_item(self.prev_button)
            self.add_item(self.next_button)
            self.add_item(self.back_button)
            self.update_buttons()
            
            embed = self.get_embed()
            await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="⬅ Back to Countries", style=discord.ButtonStyle.danger, custom_id="iptv_back_countries", row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.is_language:
            # Reconstruct CategorySelect options
            channels = await IPTVManager.get_channels_filtered(
                language=self.language,
                country=self.country_code
            )
            categories_set = {ch.get("category") for ch in channels if ch.get("category")}
            options = []
            options.append(discord.SelectOption(label="All Categories", value="any", emoji="📁"))
            for cat in sorted(list(categories_set)):
                if cat and cat.lower() != "undefined":
                    options.append(discord.SelectOption(label=cat, value=cat, emoji="🏷️"))
            if len(options) > 25:
                options = options[:25]
                
            view = IPTVCategorySelectView(self.user_id, self.language, self.country_code, options)
            embed = discord.Embed(
                title=f"📺 IPTV Category Selection ({self.language})",
                description=f"Select a category for channels in **{self.language}**.",
                color=discord.Color.blue()
            )
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            view = CountrySelectView(self.user_id)
            embed = discord.Embed(
                title="📺 IPTV Country Selection",
                description="Select a country from the dropdown below to view its live television feeds.",
                color=discord.Color.blue()
            )
            await interaction.edit_original_response(embed=embed, view=view)
