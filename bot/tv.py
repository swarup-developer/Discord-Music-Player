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
CACHE_FILE = "iptv_cache_v2.json"  # v2: real languages, EXTVLCOPT headers
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

# Reverse mapping ("india" -> "in"). get_channels_by_country() and
# get_channels_filtered() used this name without it ever being defined, so
# picking a country in /tv browse or passing country= to /tv play crashed.
COUNTRY_NAME_TO_CODE = {name.lower(): code for code, name in COUNTRY_CODES.items()}

# Stream health: iptv-org lists thousands of public streams and a share of them
# is always offline. Every channel is probed in the background and dead ones
# are hidden from browse/search/play. Results persist across restarts.
HEALTH_FILE = "iptv_health_v2.json"  # v2: probes follow master -> variant playlist
HEALTH_TTL = 12 * 60 * 60        # re-probe a channel after 12 hours
PLAYLIST_REFRESH = 24 * 60 * 60  # re-download the iptv-org playlists daily
PROBE_CONCURRENCY = 12
_RESOLUTION_RE = re.compile(r"\s*\((\d{3,4})[pi]\)")


class IPTVManager:
    _channels: List[Dict[str, Any]] = []
    _countries: List[str] = []
    _channels_by_country: Dict[str, List[Dict[str, Any]]] = {}
    _channels_by_language: Dict[str, List[Dict[str, Any]]] = {}
    _headers_by_url: Dict[str, Dict[str, str]] = {}
    _health: Dict[str, Dict[str, Any]] = {}
    _is_loading: bool = False
    _loaded_at: float = 0.0

    # ---------- health ----------
    @classmethod
    def _load_health(cls) -> None:
        try:
            with open(HEALTH_FILE, "r", encoding="utf-8") as f:
                cls._health = json.load(f)
        except (OSError, ValueError):
            cls._health = {}

    @classmethod
    def _save_health(cls) -> None:
        try:
            tmp = HEALTH_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cls._health, f)
            os.replace(tmp, HEALTH_FILE)
        except OSError as e:
            logger.warning(f"Could not save IPTV health cache: {e}")

    @classmethod
    def is_dead(cls, ch: Dict[str, Any]) -> bool:
        h = cls._health.get(ch.get("url", ""))
        return bool(h) and not h.get("ok")

    @classmethod
    def usable(cls, channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drop channels whose stream was found dead; verified-alive ones first."""
        alive = [ch for ch in channels if not cls.is_dead(ch)]
        return sorted(alive, key=lambda ch: 0 if cls._health.get(ch.get("url", ""), {}).get("ok") else 1)

    @classmethod
    def headers_for_url(cls, url: str) -> Dict[str, str]:
        """Extra HTTP headers a stream needs (from #EXTVLCOPT in the playlist)."""
        return cls._headers_by_url.get(url, {})

    @classmethod
    async def _probe(cls, client: httpx.AsyncClient, ch: Dict[str, Any]) -> bool:
        headers = {"User-Agent": ch.get("user_agent") or "Mozilla/5.0"}
        if ch.get("referrer"):
            headers["Referer"] = ch["referrer"]
        try:
            async with client.stream("GET", ch["url"], headers=headers) as resp:
                if resp.status_code != 200:
                    return False
                head = b""
                async for chunk in resp.aiter_bytes():
                    head += chunk
                    if len(head) >= 2048:
                        break
            if b"#EXTM3U" not in head[:512]:
                return len(head) >= 2048  # direct stream: real media bytes
            # A master playlist can answer while the stream inside it is gone
            # (HTTP 404), so follow it to the first variant playlist.
            text = head.decode("utf-8", errors="ignore")
            if "#EXT-X-STREAM-INF" in text:
                variant = next((l.strip() for l in text.splitlines()
                                if l.strip() and not l.startswith("#")), None)
                if variant:
                    vresp = await client.get(str(httpx.URL(ch["url"]).join(variant)), headers=headers)
                    return vresp.status_code == 200 and "#EXTM3U" in vresp.text[:512]
            return True
        except Exception:
            return False

    @classmethod
    async def health_loop(cls) -> None:
        """Keep the channel list fresh and probe streams, India first."""
        cls._load_health()
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)
        while True:
            try:
                if not cls._channels or time.time() - cls._loaded_at > PLAYLIST_REFRESH:
                    await cls.load_channels(force_refresh=bool(cls._channels))
                now = time.time()
                stale = [ch for ch in cls._channels
                         if now - cls._health.get(ch["url"], {}).get("t", 0) > HEALTH_TTL]
                stale.sort(key=lambda ch: 0 if ch.get("country_code") == "in" else 1)
                if stale:
                    logger.info(f"IPTV health check: probing {len(stale)} streams...")
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                    async def run(ch):
                        async with sem:
                            ok = await cls._probe(client, ch)
                        cls._health[ch["url"]] = {"ok": ok, "t": time.time()}
                    for i in range(0, len(stale), 200):
                        await asyncio.gather(*(run(ch) for ch in stale[i:i + 200]))
                        cls._save_health()
                if stale:
                    alive = sum(1 for ch in cls._channels if cls._health.get(ch["url"], {}).get("ok"))
                    logger.info(f"IPTV health check done: {alive}/{len(cls._channels)} streams alive.")
            except Exception as e:
                logger.error(f"IPTV health loop error: {e}")
            await asyncio.sleep(60 * 60)

    @classmethod
    async def get_channels(cls) -> List[Dict[str, Any]]:
        if not cls._channels:
            await cls.load_channels()
        return cls.usable(cls._channels)

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
            return cls.usable(cls._channels_by_country.get(code, []))
            
        results = []
        for ch in cls._channels:
            if ch.get("country_code") == normalized or ch.get("category", "").lower() == normalized:
                results.append(ch)
        return cls.usable(results)

    @classmethod
    async def get_channels_by_language(cls, language: str) -> List[Dict[str, Any]]:
        if not cls._channels:
            await cls.load_channels()
        normalized = language.strip().lower()
        return cls.usable([ch for ch in cls._channels if ch.get("language", "").lower() == normalized])

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
            
        return cls.usable(results)

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
            wait_count = 0
            while cls._is_loading and wait_count < 10:
                await asyncio.sleep(0.5)
                wait_count += 1
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
                                cls._loaded_at = mtime
                                cls._rebuild_indices()
                                logger.info(f"Loaded {len(cls._channels)} IPTV channels from local cache.")
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

                # Fetch India-specific IPTV playlist directly
                try:
                    logger.info("Fetching Indian IPTV channels from iptv-org...")
                    response_in = await client.get("https://iptv-org.github.io/iptv/countries/in.m3u", timeout=15.0)
                    if response_in.status_code == 200:
                        in_channels = cls._parse_m3u(response_in.text, is_lang=False)
                        # Take each channel's real language from the language
                        # index; every Indian channel used to be labelled Hindi.
                        lang_by_url = {ch["url"]: ch.get("language", "") for ch in cls._channels}
                        for ch in in_channels:
                            ch["country_code"] = "in"
                            ch["language"] = ch.get("language") or lang_by_url.get(ch["url"], "")
                        existing_urls = {ch["url"] for ch in cls._channels}
                        new_in_channels = [ch for ch in in_channels if ch["url"] not in existing_urls]
                        cls._channels = new_in_channels + cls._channels
                        logger.info(f"Loaded {len(in_channels)} specific Indian IPTV channels.")
                except Exception as ie:
                    logger.warning(f"Failed to fetch India IPTV playlist: {ie}")
                    
                cls._loaded_at = time.time()
                cls._rebuild_indices()
                # Write to cache
                try:
                    with open(CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump(cls._channels, f, indent=4)
                    logger.info("Saved IPTV channels to local cache.")
                except Exception as e:
                    logger.error(f"Failed to save IPTV cache: {e}")
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
            elif line.startswith("#EXTVLCOPT:") and current_info is not None:
                # Some streams only answer with a specific User-Agent/Referer.
                key, _, value = line[len("#EXTVLCOPT:"):].partition("=")
                if key == "http-user-agent" and value:
                    current_info["user_agent"] = value
                elif key == "http-referrer" and value:
                    current_info["referrer"] = value
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
        cls._headers_by_url = {}
        for ch in cls._channels:
            hdr = {}
            if ch.get("user_agent"):
                hdr["User-Agent"] = ch["user_agent"]
            if ch.get("referrer"):
                hdr["Referer"] = ch["referrer"]
            if hdr:
                cls._headers_by_url[ch["url"]] = hdr
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

def best_channel_match(channels: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    """Exact name first, then prefix, then substring; higher resolution wins ties."""
    q = query.strip().lower()

    def base(name: str) -> str:
        return _RESOLUTION_RE.sub("", name).replace("[Geo-blocked]", "").strip().lower()

    def res(name: str) -> int:
        m = _RESOLUTION_RE.search(name)
        return int(m.group(1)) if m else 0

    ranked = []
    for ch in channels:
        b = base(ch["name"])
        if b == q:
            rank = 0
        elif b.startswith(q):
            rank = 1
        elif q in b:
            rank = 2
        else:
            continue
        ranked.append((rank, ch.get("geo_blocked", False), -res(ch["name"]), ch["name"], ch))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[:4])
    return ranked[0][4]


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
        logger.info(f"IPTVCountrySelect: selected_country={selected_country}, lang={self.view.selected_lang}")
        
        # Get channels for this language and country
        channels = await IPTVManager.get_channels_filtered(
            language=self.view.selected_lang,
            country=selected_country
        )
        logger.info(f"IPTVCountrySelect: found {len(channels)} channels for lang={self.view.selected_lang}, country={selected_country}")
        
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
        logger.info(f"IPTVCategorySelect: selected_category={selected_category}, country={self.view.selected_country}, lang={self.view.selected_lang}")
        
        # Get final channels list
        channels = await IPTVManager.get_channels_filtered(
            language=self.view.selected_lang,
            country=self.view.selected_country,
            category=selected_category
        )
        logger.info(f"IPTVCategorySelect: found {len(channels)} final channels")
        
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
        for idx, ch in enumerate(page_channels):
            name = ch["name"]
            if len(name) > 80:
                name = name[:77] + "..."
            
            geo_emoji = "⚠️" if ch.get("geo_blocked") else "📺"
            category = ch.get("category", "General")
            
            # Use string representation of the index in the full channels list as the select value
            # to stay within Discord's 100 character SelectOption value limit
            actual_index = start + idx
            
            options.append(
                discord.SelectOption(
                    label=name,
                    value=str(actual_index),
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
        try:
            selected_index = int(self.values[0])
            channel_data = self.channels[selected_index]
            selected_url = channel_data["url"]
        except (ValueError, IndexError):
            return
            
        # Find chosen channel details
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
            # _play_audio reports "Now playing" or the error itself.
            await music_cog._play_audio(selected_url, interaction, title=title)


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
