import discord
from discord.ext import commands


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _build_help_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Bot Help", color=discord.Color.blurple())
        embed.description = (
            "1. Join a voice channel.\n"
            "2. Run `/search <song>` or `/play <song>`.\n"
            "3. Click the interactive number buttons (1 to 10) on the search result to play.\n"
            "4. Use `/skip` to move through the queue.\n"
            "5. Use `/stop` or `/go` when you are done."
        )
        embed.add_field(
            name="Music",
            value=(
                "`/join` join your voice channel\n"
                "`/play <song>` search and select a track\n"
                "`/search <query>` search and select a track"
            ),
            inline=False,
        )
        embed.add_field(
            name="Playback",
            value=(
                "`/volume <1-200>` set playback volume\n"
                "`/skip` skip to the next queued track\n"
                "`/pause` pause playback\n"
                "`/resume` resume playback\n"
                "`/stop` stop the current track\n"
                "`/go` disconnect from voice\n"
                "`/diagnose` check common voice issues"
            ),
            inline=False,
        )
        return embed

    @discord.app_commands.command(name="help", description="Show categorized bot commands")
    async def help(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self._build_help_embed(), ephemeral=True)

