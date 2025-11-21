import os
import asyncio
import logging
from typing import Optional
import re

import discord
from discord import app_commands
from discord.ext import commands

import yt_dlp
from dotenv import load_dotenv

# ------- Configuration -------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Please set DISCORD_TOKEN in your .env file.")

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("musicbot")

# YT-DLP options
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "skip_download": True,
    "extract_flat": False,
    "extractor_args": {"youtube": {"player_client": ["default"]}},
}

# FFmpeg options for streaming
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)

# ------- Helper classes & functions -------
class Song:
    def __init__(self, title: str, webpage_url: str, requester: discord.Member, duration: Optional[int]=None):
        self.title = title
        self.webpage_url = webpage_url
        self.requester = requester
        self.duration = duration

    def __str__(self):
        return f"{self.title} ({self.webpage_url})"

async def search_youtube(query: str):
    """
    Use yt-dlp to search or extract info from the given query.
    """
    loop = asyncio.get_running_loop()
    to_search = query

    if "open.spotify.com/track" in query or "spotify:track:" in query or "music.apple.com" in query:
        try:
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            artist = info.get('artist') or info.get('uploader') or ""
            title = info.get('title') or ""
            if title:
                search_term = f"{artist} - {title}".strip()
                to_search = f"ytsearch1:{search_term}"
        except Exception:
            to_search = f"ytsearch1:{query}"
    else:
        if not re.match(r'https?://', query):
            to_search = f"ytsearch1:{query}"

    try:
        info = await loop.run_in_executor(None, lambda: ytdl.extract_info(to_search, download=False))
    except Exception as e:
        log.exception("yt-dlp extract failed")
        raise RuntimeError("Failed to retrieve info from source.") from e

    if "entries" in info and info["entries"]:
        entry = info["entries"][0]
    else:
        entry = info

    webpage_url = entry.get("webpage_url") or entry.get("url")
    title = entry.get("title", "Unknown title")
    duration = entry.get("duration")

    return {"title": title, "webpage_url": webpage_url, "duration": duration}

def create_ffmpeg_source(url: str):
    return discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)

# ------- Bot & Player State -------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

guild_queues: dict[int, asyncio.Queue] = {}
guild_players: dict[int, asyncio.Task] = {}
guild_autoplay: dict[int, bool] = {}

async def ensure_queue(guild_id: int):
    if guild_id not in guild_queues:
        guild_queues[guild_id] = asyncio.Queue()
    if guild_id not in guild_autoplay:
        guild_autoplay[guild_id] = False
    return guild_queues[guild_id]

# Configuration: Auto-disconnect settings
INACTIVITY_TIMEOUT = 300  # 5 minutes of no songs playing
ALONE_TIMEOUT = 60  # 1 minute if bot is alone in voice channel

# Player loop per-guild with reconnection handling
async def player_loop(guild: discord.Guild, voice_client: discord.VoiceClient):
    guild_id = guild.id
    queue = guild_queues[guild_id]
    log.info(f"Starting player loop for guild {guild.name} ({guild.id})")

    while True:
        try:
            # Wait for next song with timeout
            song: Song = await asyncio.wait_for(queue.get(), timeout=INACTIVITY_TIMEOUT)
        except asyncio.TimeoutError:
            # No songs added for INACTIVITY_TIMEOUT seconds
            log.info(f"Queue inactive for {INACTIVITY_TIMEOUT}s in guild {guild.id}, disconnecting...")
            if voice_client.is_connected():
                await voice_client.disconnect()
            del guild_players[guild_id]
            break
        except asyncio.CancelledError:
            break

        # Check if bot is alone in voice channel
        if voice_client.is_connected() and voice_client.channel:
            # Count non-bot members in the voice channel
            members = [m for m in voice_client.channel.members if not m.bot]
            if len(members) == 0:
                log.info(f"Bot is alone in voice channel, waiting {ALONE_TIMEOUT}s...")
                await asyncio.sleep(ALONE_TIMEOUT)
                # Check again after timeout
                members = [m for m in voice_client.channel.members if not m.bot]
                if len(members) == 0:
                    log.info(f"Still alone after {ALONE_TIMEOUT}s, disconnecting...")
                    if voice_client.is_connected():
                        await voice_client.disconnect()
                    del guild_players[guild_id]
                    break

        # Check if voice client is still connected
        if not voice_client.is_connected():
            log.warning(f"Voice client disconnected for guild {guild.id}, attempting reconnect...")
            try:
                # Try to reconnect to the channel
                channel = song.requester.voice.channel if song.requester.voice else None
                if channel:
                    voice_client = await channel.connect()
                    guild.voice_client = voice_client
                else:
                    log.error("Cannot reconnect: requester not in voice channel")
                    continue
            except Exception as e:
                log.error(f"Failed to reconnect: {e}")
                continue

        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(song.webpage_url, download=False))
            stream_url = info.get('url') or song.webpage_url
        except Exception:
            log.exception("Failed extracting info before playback, skipping track.")
            continue

        source = create_ffmpeg_source(stream_url)
        done_event = asyncio.Event()

        def after_play(err):
            if err:
                log.error(f"Player error: {err}")
            bot.loop.call_soon_threadsafe(done_event.set)

        try:
            voice_client.play(source, after=after_play)
        except Exception as e:
            log.error(f"Failed to play audio: {e}")
            queue.task_done()
            continue

        await done_event.wait()

        # Autoplay logic
        if guild_autoplay.get(guild_id, False):
            search_q = f"ytsearch1:{song.title} related"
            try:
                info_rel = await loop.run_in_executor(None, lambda: ytdl.extract_info(search_q, download=False))
                if "entries" in info_rel and info_rel["entries"]:
                    entry = info_rel["entries"][0]
                    if entry.get("webpage_url") and entry.get("webpage_url") != song.webpage_url:
                        autoplay_song = Song(entry.get("title"), entry.get("webpage_url"), song.requester, entry.get("duration"))
                        await queue.put(autoplay_song)
            except Exception:
                log.debug("autoplay search failed; continuing.")

        queue.task_done()

# ------- Event Handlers -------
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

# ------- Slash commands -------
@bot.tree.command(name="play", description="Play a song from YouTube/Spotify/Apple (or search by name).")
@app_commands.describe(query="Song name or URL")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("You must be in a voice channel to use /play.")

    voice_channel = interaction.user.voice.channel
    guild = interaction.guild
    await ensure_queue(guild.id)

    try:
        info = await search_youtube(query)
    except Exception as e:
        return await interaction.followup.send(f"Search failed: {e}")

    song = Song(title=info["title"], webpage_url=info["webpage_url"], requester=interaction.user, duration=info.get("duration"))

    vc: discord.VoiceClient = guild.voice_client
    if not vc or not vc.is_connected():
        try:
            vc = await voice_channel.connect(timeout=10.0, reconnect=True)
        except asyncio.TimeoutError:
            return await interaction.followup.send("Failed to connect to voice channel (timeout).")
        except Exception as e:
            return await interaction.followup.send(f"Failed to connect: {e}")
    else:
        if vc.channel != voice_channel:
            await vc.move_to(voice_channel)

    await guild_queues[guild.id].put(song)

    if guild.id not in guild_players or guild_players[guild.id].done():
        guild_players[guild.id] = bot.loop.create_task(player_loop(guild, vc))

    await interaction.followup.send(f"Queued **{song.title}** (requested by {interaction.user.display_name})")

@bot.tree.command(name="skipcurrent", description="Skip the currently playing track.")
async def skipcurrent(interaction: discord.Interaction):
    guild = interaction.guild
    vc = guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("Nothing is playing right now.")
    vc.stop()
    await interaction.response.send_message("Skipped current track.")

@bot.tree.command(name="pause", description="Pause the current track.")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("Nothing is playing.")
    vc.pause()
    await interaction.response.send_message("Paused.")

@bot.tree.command(name="resume", description="Resume playback.")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        return await interaction.response.send_message("Nothing is paused.")
    vc.resume()
    await interaction.response.send_message("Resumed.")

@bot.tree.command(name="queue", description="Show the current queue.")
async def show_queue(interaction: discord.Interaction):
    await interaction.response.defer()
    q = guild_queues.get(interaction.guild.id)
    if not q or q.empty():
        return await interaction.followup.send("Queue is empty.")
    items = list(q._queue)
    if not items:
        return await interaction.followup.send("Queue is empty.")
    lines = [f"{idx+1}. {it.title} — requested by {it.requester.display_name}" for idx, it in enumerate(items)]
    await interaction.followup.send("Current queue:\n" + "\n".join(lines[:10]))

@bot.tree.command(name="clearqueue", description="Clear the queue.")
async def clear_queue(interaction: discord.Interaction):
    q = guild_queues.get(interaction.guild.id)
    if not q:
        return await interaction.response.send_message("Queue is already empty.")

    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
    await interaction.response.send_message("Queue cleared.")

@bot.tree.command(name="autoplay", description="Toggle autoplay (auto-enqueue related tracks).")
async def autoplay_toggle(interaction: discord.Interaction):
    gid = interaction.guild.id
    await ensure_queue(gid)
    guild_autoplay[gid] = not guild_autoplay.get(gid, False)
    await interaction.response.send_message(f"Autoplay is now {'ON' if guild_autoplay[gid] else 'OFF'}.")

@bot.tree.command(name="leave", description="Make the bot leave the voice channel.")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        return await interaction.response.send_message("I'm not in a voice channel.")
    task = guild_players.get(interaction.guild.id)
    if task:
        task.cancel()
    await vc.disconnect()
    await interaction.response.send_message("Left the voice channel.")

@bot.tree.command(name="about", description="About the bot.")
async def about(interaction: discord.Interaction):
    await interaction.response.send_message("Music bot example — plays audio from YouTube. Use responsibly and respect TOS.")

@bot.tree.command(name="help", description="Show help for commands.")
async def help_cmd(interaction: discord.Interaction):
    txt = (
        "/play <query_or_url> — search and play (queues if already playing)\n"
        "/skipcurrent — skip current track\n"
        "/pause — pause playback\n"
        "/resume — resume playback\n"
        "/queue — show queue\n"
        "/clearqueue — clear the current queue\n"
        "/autoplay — toggle autoplay on/off\n"
        "/leave — disconnect the bot\n"
        "/about — info about the bot\n"
    )
    await interaction.response.send_message(txt)

# Run the bot with reconnect enabled
if __name__ == "__main__":
    bot.run(TOKEN, reconnect=True)
