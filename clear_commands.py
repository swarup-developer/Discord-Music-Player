import asyncio

import discord

from bot.app import build_bot
from bot.config import BOT_SYNC_GUILD_ID, DISCORD_TOKEN


async def main() -> None:
    if not DISCORD_TOKEN:
        raise ValueError("Missing DISCORD_TOKEN in environment variables")

    bot = build_bot()
    await bot.login(DISCORD_TOKEN)
    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()

        if BOT_SYNC_GUILD_ID:
            guild = discord.Object(id=int(BOT_SYNC_GUILD_ID))
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
