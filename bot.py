import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))  # your server ID

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Load your cogs (NON-package style)
        await self.load_extension("cogs.bias")
        await self.load_extension("cogs.current")
        await bot.load_extension("cogs.hourback")

        # Fast guild sync (commands appear instantly)
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        print("Loaded cogs + synced slash commands.")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing (check your .env).")

bot = MyBot()
bot.run(TOKEN)
