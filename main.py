import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ------- Environment -------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Please set DISCORD_TOKEN in your .env file.")

# ------- Logging -------
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("musicbot")

# ------- Bot setup -------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

# ------- Inject bot reference into modules that need it -------
# These modules store `bot = None` at the top and have it set here
# at startup to avoid circular imports while still sharing one bot instance.
import helpers
import player
import ui

helpers.bot = bot
player.bot = bot
ui.bot = bot

# ------- Register all slash commands -------
from commands import register_commands
register_commands(bot)


# ------- Events -------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} commands.")
    except Exception as e:
        log.warning("Failed to sync application commands: %s", e)
    print("Bot ready.")


@bot.event
async def on_resumed():
    log.info("Bot session resumed after disconnect")


@bot.event
async def on_disconnect():
    log.warning("Bot disconnected from Discord")


@bot.event
async def on_connect():
    log.info("Bot connected to Discord")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Detect when the bot itself is disconnected from a voice channel."""
    if member.id != bot.user.id:
        return
    if before.channel and not after.channel:
        log.info(f"Bot disconnected from voice in guild {before.channel.guild.id}")


# ------- Entry point -------
if __name__ == "__main__":
    bot.run(TOKEN, reconnect=True)
