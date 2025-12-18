import os
import asyncio
import logging
from typing import Optional
import re
import time

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

# YT-DLP options for multi-result search
YTDL_SEARCH_OPTS = {
    **YTDL_OPTS,
    "extract_flat": True,
}

# FFmpeg options for streaming (low latency)
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 0 -probesize 32",
    "options": "-vn -bufsize 64k"
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)
ytdl_search = yt_dlp.YoutubeDL(YTDL_SEARCH_OPTS)

# Configuration: Auto-disconnect settings
INACTIVITY_TIMEOUT = 1800  # 30 minutes of no songs playing
ALONE_TIMEOUT = 60  # 1 minute if bot is alone in voice channel
RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY = 2  # seconds between reconnect attempts

# ------- Helper classes & functions -------
class Song:
    def __init__(self, title: str, webpage_url: str, requester: discord.Member,
                 duration: Optional[int] = None, thumbnail: Optional[str] = None):
        self.title = title
        self.webpage_url = webpage_url
        self.requester = requester
        self.duration = duration
        self.thumbnail = thumbnail
        self.start_time: Optional[float] = None

    def __str__(self):
        return f"{self.title} ({self.webpage_url})"


def format_duration(seconds: Optional[int]) -> str:
    """Format duration in seconds to mm:ss or hh:mm:ss."""
    if seconds is None:
        return "Unknown"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def create_progress_bar(current: float, total: float, length: int = 12) -> str:
    """Create a text-based progress bar."""
    if total <= 0:
        return "[" + "-" * length + "]"
    progress = min(current / total, 1.0)
    filled = int(length * progress)
    return "[" + "=" * filled + ">" + "-" * (length - filled - 1) + "]"


async def search_youtube(query: str, max_results: int = 1):
    """
    Use yt-dlp to search or extract info from the given query.
    Returns a list of results if max_results > 1.
    """
    loop = asyncio.get_running_loop()
    to_search = query

    # Handle Spotify/Apple Music - convert to YouTube search
    if "open.spotify.com/track" in query or "spotify:track:" in query or "music.apple.com" in query:
        try:
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            artist = info.get('artist') or info.get('uploader') or ""
            title = info.get('title') or ""
            if title:
                search_term = f"{artist} - {title}".strip()
                to_search = f"ytsearch{max_results}:{search_term}"
        except Exception:
            to_search = f"ytsearch{max_results}:{query}"
    # Handle SoundCloud URLs - pass directly
    elif "soundcloud.com" in query:
        to_search = query
    # Handle other URLs - pass directly
    elif re.match(r'https?://', query):
        to_search = query
    # Text search - use ytsearch
    else:
        to_search = f"ytsearch{max_results}:{query}"

    try:
        if max_results > 1 and not re.match(r'https?://', query):
            info = await loop.run_in_executor(None, lambda: ytdl_search.extract_info(to_search, download=False))
        else:
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(to_search, download=False))
    except Exception as e:
        log.exception("yt-dlp extract failed")
        raise RuntimeError(f"Failed to retrieve info: {e}") from e

    # Return multiple results if requested
    if max_results > 1 and "entries" in info and info["entries"]:
        results = []
        for entry in info["entries"][:max_results]:
            if entry:
                results.append({
                    "title": entry.get("title", "Unknown title"),
                    "webpage_url": entry.get("webpage_url") or entry.get("url"),
                    "duration": entry.get("duration"),
                    "thumbnail": entry.get("thumbnail"),
                })
        return results

    # Single result
    if "entries" in info and info["entries"]:
        entry = info["entries"][0]
    else:
        entry = info

    return [{
        "title": entry.get("title", "Unknown title"),
        "webpage_url": entry.get("webpage_url") or entry.get("url"),
        "duration": entry.get("duration"),
        "thumbnail": entry.get("thumbnail"),
    }]


async def get_stream_url(webpage_url: str) -> tuple[str, dict]:
    """Extract fresh stream URL with retry logic."""
    loop = asyncio.get_running_loop()
    for attempt in range(RECONNECT_ATTEMPTS):
        try:
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(webpage_url, download=False))
            stream_url = info.get('url') or webpage_url
            return stream_url, info
        except Exception as e:
            log.warning(f"Stream extraction attempt {attempt + 1} failed: {e}")
            if attempt < RECONNECT_ATTEMPTS - 1:
                await asyncio.sleep(RECONNECT_DELAY * (attempt + 1))
    raise RuntimeError("Failed to extract stream URL after retries")


def create_ffmpeg_source(url: str):
    return discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)


# ------- Bot & Player State -------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

# Per-guild state
guild_queues: dict[int, asyncio.Queue] = {}
guild_players: dict[int, asyncio.Task] = {}
guild_autoplay: dict[int, bool] = {}
guild_loop_mode: dict[int, str] = {}  # "off", "track"
guild_loop_song: dict[int, Optional[Song]] = {}
guild_pending_loop_url: dict[int, Optional[str]] = {}  # URL that should start looping when reached
guild_current_song: dict[int, Optional[Song]] = {}
guild_now_playing_msg: dict[int, Optional[discord.Message]] = {}
guild_text_channel: dict[int, Optional[discord.TextChannel]] = {}
guild_last_voice_channel: dict[int, Optional[discord.VoiceChannel]] = {}


async def ensure_queue(guild_id: int):
    if guild_id not in guild_queues:
        guild_queues[guild_id] = asyncio.Queue()
    if guild_id not in guild_autoplay:
        guild_autoplay[guild_id] = False
    if guild_id not in guild_loop_mode:
        guild_loop_mode[guild_id] = "off"
    return guild_queues[guild_id]


# ------- Error Embed Helper -------
def create_error_embed(title: str, description: str) -> discord.Embed:
    """Create a user-friendly error embed."""
    embed = discord.Embed(
        title=f"Error: {title}",
        description=description,
        color=discord.Color.red()
    )
    return embed


# ------- Now Playing Embed -------
def create_now_playing_embed(song: Song, loop_mode: str = "off", is_paused: bool = False) -> discord.Embed:
    """Create a rich Now Playing embed."""
    embed = discord.Embed(
        title="Now Playing" if not is_paused else "Paused",
        description=f"[{song.title}]({song.webpage_url})",
        color=discord.Color.blurple() if not is_paused else discord.Color.orange()
    )

    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    # Duration field
    duration_str = format_duration(song.duration)
    embed.add_field(name="Duration", value=duration_str, inline=True)

    # Requested by
    embed.add_field(name="Requested by", value=song.requester.display_name, inline=True)

    # Loop status
    loop_status = "Off" if loop_mode == "off" else "Track"
    embed.add_field(name="Loop", value=loop_status, inline=True)

    return embed


# ------- UI Components -------
class MusicControlView(discord.ui.View):
    """Persistent buttons for music control."""

    def __init__(self, guild_id: int, is_paused: bool = False):
        super().__init__(timeout=None)
        self.guild_id = guild_id

        # Update button states based on current state
        for item in self.children:
            if hasattr(item, 'custom_id'):
                if item.custom_id == "music_pause" and is_paused:
                    item.label = "Paused"
                    item.emoji = "▶️"
                    item.style = discord.ButtonStyle.success
                elif item.custom_id == "music_loop":
                    if guild_loop_mode.get(guild_id) == "track":
                        item.style = discord.ButtonStyle.success

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️", custom_id="music_pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("Not connected to voice.", ephemeral=True)

        if vc.is_playing():
            vc.pause()
            button.label = "Paused"
            button.emoji = "▶️"
            button.style = discord.ButtonStyle.success
            # Update embed
            song = guild_current_song.get(self.guild_id)
            if song:
                loop_mode = guild_loop_mode.get(self.guild_id, "off")
                embed = create_now_playing_embed(song, loop_mode, is_paused=True)
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.response.edit_message(view=self)
        elif vc.is_paused():
            vc.resume()
            button.label = "Pause"
            button.emoji = "⏸️"
            button.style = discord.ButtonStyle.secondary
            # Update embed
            song = guild_current_song.get(self.guild_id)
            if song:
                loop_mode = guild_loop_mode.get(self.guild_id, "off")
                embed = create_now_playing_embed(song, loop_mode, is_paused=False)
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("Nothing to pause/resume.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, emoji="⏭️", custom_id="music_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)

        # Disable loop when skipping
        guild_loop_mode[self.guild_id] = "off"
        guild_loop_song[self.guild_id] = None
        vc.stop()
        await interaction.response.send_message("Skipped!", ephemeral=True)

    @discord.ui.button(label="Stop and Clear Queue", style=discord.ButtonStyle.danger, emoji="⏹️", custom_id="music_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)

        # Clear queue
        q = guild_queues.get(self.guild_id)
        if q:
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # Clear loop
        guild_loop_mode[self.guild_id] = "off"
        guild_loop_song[self.guild_id] = None

        # Stop playback (but stay connected)
        vc.stop()
        await interaction.response.send_message("Stopped playback and cleared queue.", ephemeral=True)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="music_loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_mode = guild_loop_mode.get(self.guild_id, "off")
        song = guild_current_song.get(self.guild_id)

        if current_mode == "off":
            guild_loop_mode[self.guild_id] = "track"
            guild_loop_song[self.guild_id] = song
            button.style = discord.ButtonStyle.success
            status = "Loop enabled for current track!"
        else:
            guild_loop_mode[self.guild_id] = "off"
            guild_loop_song[self.guild_id] = None
            button.style = discord.ButtonStyle.secondary
            status = "Loop disabled."

        # Update embed
        if song:
            vc = interaction.guild.voice_client
            is_paused = vc.is_paused() if vc else False
            embed = create_now_playing_embed(song, guild_loop_mode.get(self.guild_id, "off"), is_paused)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message(status, ephemeral=True)


class SearchResultSelect(discord.ui.Select):
    """Dropdown for selecting from search results."""

    def __init__(self, results: list[dict], requester: discord.Member, voice_channel: discord.VoiceChannel):
        self.results = results
        self.requester = requester
        self.voice_channel = voice_channel

        options = []
        for i, result in enumerate(results[:5]):
            title = result["title"][:100]  # Discord limit
            duration = format_duration(result.get("duration"))
            options.append(discord.SelectOption(
                label=title[:100],
                description=f"Duration: {duration}",
                value=str(i)
            ))

        super().__init__(
            placeholder="Choose a song...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        idx = int(self.values[0])
        result = self.results[idx]

        guild = interaction.guild
        await ensure_queue(guild.id)

        # Get full info for the selected track
        try:
            full_results = await search_youtube(result["webpage_url"], max_results=1)
            info = full_results[0]
        except Exception as e:
            return await interaction.followup.send(embed=create_error_embed("Search Failed", str(e)))

        song = Song(
            title=info["title"],
            webpage_url=info["webpage_url"],
            requester=self.requester,
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail")
        )

        vc: discord.VoiceClient = guild.voice_client
        if not vc or not vc.is_connected():
            try:
                vc = await self.voice_channel.connect(timeout=10.0, reconnect=True)
            except Exception as e:
                return await interaction.followup.send(embed=create_error_embed("Connection Failed", str(e)))

        await guild_queues[guild.id].put(song)
        guild_text_channel[guild.id] = interaction.channel
        guild_last_voice_channel[guild.id] = self.voice_channel

        if guild.id not in guild_players or guild_players[guild.id].done():
            guild_players[guild.id] = bot.loop.create_task(player_loop(guild, vc))

        # Update the original message
        embed = discord.Embed(
            title="Added to Queue",
            description=f"[{song.title}]({song.webpage_url})",
            color=discord.Color.green()
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.add_field(name="Duration", value=format_duration(song.duration), inline=True)
        embed.add_field(name="Requested by", value=self.requester.display_name, inline=True)

        # Disable the select menu after selection
        self.disabled = True
        await interaction.edit_original_response(embed=embed, view=self.view)

        # Refresh Now Playing to keep it as most recent
        await refresh_now_playing(guild.id)


class SearchResultView(discord.ui.View):
    """View containing the search result selector."""

    def __init__(self, results: list[dict], requester: discord.Member, voice_channel: discord.VoiceChannel):
        super().__init__(timeout=60)
        self.add_item(SearchResultSelect(results, requester, voice_channel))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ------- Player Loop -------
async def player_loop(guild: discord.Guild, voice_client: discord.VoiceClient):
    """Main player loop per guild with enhanced error handling."""
    guild_id = guild.id
    queue = guild_queues[guild_id]
    log.info(f"Starting player loop for guild {guild.name} ({guild.id})")

    while True:
        try:
            # Wait for next song with timeout
            song: Song = await asyncio.wait_for(queue.get(), timeout=INACTIVITY_TIMEOUT)
        except asyncio.TimeoutError:
            log.info(f"Queue inactive for {INACTIVITY_TIMEOUT}s in guild {guild.id}, disconnecting...")
            await cleanup_guild(guild_id, voice_client)
            break
        except asyncio.CancelledError:
            await cleanup_guild(guild_id, voice_client, disconnect=False)
            break

        # Check if bot is alone in voice channel
        if voice_client.is_connected() and voice_client.channel:
            members = [m for m in voice_client.channel.members if not m.bot]
            if len(members) == 0:
                log.info(f"Bot is alone in voice channel, waiting {ALONE_TIMEOUT}s...")
                await asyncio.sleep(ALONE_TIMEOUT)
                members = [m for m in voice_client.channel.members if not m.bot]
                if len(members) == 0:
                    log.info(f"Still alone after {ALONE_TIMEOUT}s, disconnecting...")
                    await cleanup_guild(guild_id, voice_client)
                    break

        # Reconnect if disconnected
        voice_client = await ensure_voice_connection(guild, voice_client, song)
        if not voice_client:
            log.error(f"Could not establish voice connection for guild {guild.id}")
            continue

        # Get stream URL with retry
        try:
            stream_url, info = await get_stream_url(song.webpage_url)
            # Update song thumbnail if we got it
            if not song.thumbnail and info.get("thumbnail"):
                song.thumbnail = info.get("thumbnail")
        except Exception as e:
            log.error(f"Failed to get stream URL: {e}")
            await send_error_to_channel(guild_id, "Stream Error", f"Could not load: {song.title}")
            queue.task_done()
            continue

        # Set current song and start time
        guild_current_song[guild_id] = song
        song.start_time = time.time()

        # Check if this song should start looping (pending loop)
        if guild_pending_loop_url.get(guild_id) == song.webpage_url:
            guild_loop_mode[guild_id] = "track"
            guild_loop_song[guild_id] = song
            guild_pending_loop_url[guild_id] = None

        # Send Now Playing embed
        await send_now_playing(guild_id, song)

        # Play the track
        source = create_ffmpeg_source(stream_url)
        done_event = asyncio.Event()
        playback_error = [None]  # Use list to capture in closure

        def after_play(err):
            if err:
                log.error(f"Player error: {err}")
                playback_error[0] = err
            bot.loop.call_soon_threadsafe(done_event.set)

        try:
            voice_client.play(source, after=after_play)
        except Exception as e:
            log.error(f"Failed to play audio: {e}")
            await send_error_to_channel(guild_id, "Playback Error", f"Failed to play: {song.title}")
            queue.task_done()
            continue

        await done_event.wait()

        # Handle playback error - try re-extraction once
        if playback_error[0]:
            log.warning(f"Playback error occurred, attempting re-extraction...")
            try:
                stream_url, _ = await get_stream_url(song.webpage_url)
                source = create_ffmpeg_source(stream_url)
                done_event.clear()
                voice_client.play(source, after=after_play)
                await done_event.wait()
            except Exception as e:
                log.error(f"Re-extraction failed: {e}")
                await send_error_to_channel(guild_id, "Stream Failed", f"Skipping: {song.title}")

        # Handle loop mode
        if guild_loop_mode.get(guild_id) == "track" and guild_loop_song.get(guild_id):
            # Re-queue the same song at the front
            loop_song = guild_loop_song[guild_id]
            # Create a new song instance to reset start_time
            new_song = Song(
                title=loop_song.title,
                webpage_url=loop_song.webpage_url,
                requester=loop_song.requester,
                duration=loop_song.duration,
                thumbnail=loop_song.thumbnail
            )
            # Put at front by creating new queue with this song first
            items = []
            while not queue.empty():
                try:
                    items.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await queue.put(new_song)
            for item in items:
                await queue.put(item)

        # Autoplay logic (only if not looping)
        elif guild_autoplay.get(guild_id, False):
            search_q = f"ytsearch1:{song.title} related"
            try:
                loop = asyncio.get_running_loop()
                info_rel = await loop.run_in_executor(None, lambda: ytdl.extract_info(search_q, download=False))
                if "entries" in info_rel and info_rel["entries"]:
                    entry = info_rel["entries"][0]
                    if entry.get("webpage_url") and entry.get("webpage_url") != song.webpage_url:
                        autoplay_song = Song(
                            entry.get("title"),
                            entry.get("webpage_url"),
                            song.requester,
                            entry.get("duration"),
                            entry.get("thumbnail")
                        )
                        await queue.put(autoplay_song)
            except Exception:
                log.debug("autoplay search failed; continuing.")

        queue.task_done()
        guild_current_song[guild_id] = None


async def cleanup_guild(guild_id: int, voice_client: discord.VoiceClient, disconnect: bool = True):
    """Clean up guild state."""
    if disconnect and voice_client and voice_client.is_connected():
        await voice_client.disconnect()
    if guild_id in guild_players:
        del guild_players[guild_id]
    guild_current_song[guild_id] = None
    guild_loop_mode[guild_id] = "off"
    guild_loop_song[guild_id] = None
    guild_pending_loop_url[guild_id] = None


async def ensure_voice_connection(guild: discord.Guild, voice_client: discord.VoiceClient, song: Song) -> Optional[discord.VoiceClient]:
    """Ensure voice connection with exponential backoff retry."""
    if voice_client and voice_client.is_connected():
        return voice_client

    log.warning(f"Voice client disconnected for guild {guild.id}, attempting reconnect...")

    # Try to get channel from various sources
    channel = None
    if guild_last_voice_channel.get(guild.id):
        channel = guild_last_voice_channel[guild.id]
    elif song.requester.voice and song.requester.voice.channel:
        channel = song.requester.voice.channel

    if not channel:
        log.error("Cannot reconnect: no voice channel available")
        return None

    for attempt in range(RECONNECT_ATTEMPTS):
        try:
            delay = RECONNECT_DELAY * (2 ** attempt)  # Exponential backoff
            if attempt > 0:
                log.info(f"Reconnect attempt {attempt + 1}, waiting {delay}s...")
                await asyncio.sleep(delay)

            voice_client = await channel.connect(timeout=10.0, reconnect=True)
            guild_last_voice_channel[guild.id] = channel
            log.info(f"Reconnected to voice channel in guild {guild.id}")
            return voice_client
        except Exception as e:
            log.error(f"Reconnect attempt {attempt + 1} failed: {e}")

    return None


async def send_now_playing(guild_id: int, song: Song):
    """Send Now Playing embed with controls."""
    channel = guild_text_channel.get(guild_id)
    if not channel:
        return

    # Get voice client to check pause state
    guild = bot.get_guild(guild_id)
    vc = guild.voice_client if guild else None
    is_paused = vc.is_paused() if vc else False

    try:
        # Delete old Now Playing message
        old_msg = guild_now_playing_msg.get(guild_id)
        if old_msg:
            try:
                await old_msg.delete()
            except Exception:
                pass

        loop_mode = guild_loop_mode.get(guild_id, "off")
        embed = create_now_playing_embed(song, loop_mode, is_paused)
        view = MusicControlView(guild_id, is_paused)

        msg = await channel.send(embed=embed, view=view)
        guild_now_playing_msg[guild_id] = msg
    except Exception as e:
        log.error(f"Failed to send Now Playing: {e}")


async def refresh_now_playing(guild_id: int):
    """Resend Now Playing embed to keep it as most recent message."""
    song = guild_current_song.get(guild_id)
    if not song:
        return

    channel = guild_text_channel.get(guild_id)
    if not channel:
        return

    # Get voice client to check pause state
    guild = bot.get_guild(guild_id)
    vc = guild.voice_client if guild else None
    is_paused = vc.is_paused() if vc else False

    try:
        # Delete old Now Playing message
        old_msg = guild_now_playing_msg.get(guild_id)
        if old_msg:
            try:
                await old_msg.delete()
            except Exception:
                pass

        loop_mode = guild_loop_mode.get(guild_id, "off")
        embed = create_now_playing_embed(song, loop_mode, is_paused)
        view = MusicControlView(guild_id, is_paused)

        msg = await channel.send(embed=embed, view=view)
        guild_now_playing_msg[guild_id] = msg
    except Exception as e:
        log.error(f"Failed to refresh Now Playing: {e}")


async def send_error_to_channel(guild_id: int, title: str, description: str):
    """Send error message to the guild's text channel."""
    channel = guild_text_channel.get(guild_id)
    if channel:
        try:
            await channel.send(embed=create_error_embed(title, description))
        except Exception:
            pass


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


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Handle voice state updates for cleanup."""
    if member.id != bot.user.id:
        return

    # Bot was disconnected
    if before.channel and not after.channel:
        guild_id = before.channel.guild.id
        log.info(f"Bot disconnected from voice in guild {guild_id}")


# ------- Slash commands -------
@bot.tree.command(name="play", description="Play a song from YouTube/Spotify/Apple/SoundCloud (or search by name).")
@app_commands.describe(query="Song name or URL")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send(embed=create_error_embed("Not in Voice", "You must be in a voice channel to use /play."))

    voice_channel = interaction.user.voice.channel
    guild = interaction.guild
    await ensure_queue(guild.id)
    guild_text_channel[guild.id] = interaction.channel
    guild_last_voice_channel[guild.id] = voice_channel

    # Check if it's a URL (direct play) or search term (show results)
    is_url = bool(re.match(r'https?://', query))

    if is_url:
        # Direct URL - queue immediately
        try:
            results = await search_youtube(query, max_results=1)
            info = results[0]
        except Exception as e:
            return await interaction.followup.send(embed=create_error_embed("Search Failed", str(e)))

        song = Song(
            title=info["title"],
            webpage_url=info["webpage_url"],
            requester=interaction.user,
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail")
        )

        vc: discord.VoiceClient = guild.voice_client
        if not vc or not vc.is_connected():
            try:
                vc = await voice_channel.connect(timeout=10.0, reconnect=True)
            except asyncio.TimeoutError:
                return await interaction.followup.send(embed=create_error_embed("Timeout", "Failed to connect to voice channel."))
            except Exception as e:
                return await interaction.followup.send(embed=create_error_embed("Connection Failed", str(e)))
        else:
            if vc.channel != voice_channel:
                await vc.move_to(voice_channel)

        await guild_queues[guild.id].put(song)

        if guild.id not in guild_players or guild_players[guild.id].done():
            guild_players[guild.id] = bot.loop.create_task(player_loop(guild, vc))

        embed = discord.Embed(
            title="Added to Queue",
            description=f"[{song.title}]({song.webpage_url})",
            color=discord.Color.green()
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.add_field(name="Duration", value=format_duration(song.duration), inline=True)
        embed.add_field(name="Requested by", value=interaction.user.display_name, inline=True)
        await interaction.followup.send(embed=embed)

        # Refresh Now Playing to keep it as most recent
        await refresh_now_playing(guild.id)
    else:
        # Search term - show selection
        try:
            results = await search_youtube(query, max_results=5)
        except Exception as e:
            return await interaction.followup.send(embed=create_error_embed("Search Failed", str(e)))

        if not results:
            return await interaction.followup.send(embed=create_error_embed("No Results", "No songs found for your search."))

        # If only 1 result, queue directly
        if len(results) == 1:
            info = results[0]
            song = Song(
                title=info["title"],
                webpage_url=info["webpage_url"],
                requester=interaction.user,
                duration=info.get("duration"),
                thumbnail=info.get("thumbnail")
            )

            vc: discord.VoiceClient = guild.voice_client
            if not vc or not vc.is_connected():
                try:
                    vc = await voice_channel.connect(timeout=10.0, reconnect=True)
                except Exception as e:
                    return await interaction.followup.send(embed=create_error_embed("Connection Failed", str(e)))

            await guild_queues[guild.id].put(song)

            if guild.id not in guild_players or guild_players[guild.id].done():
                guild_players[guild.id] = bot.loop.create_task(player_loop(guild, vc))

            embed = discord.Embed(
                title="Added to Queue",
                description=f"[{song.title}]({song.webpage_url})",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)

            # Refresh Now Playing to keep it as most recent
            await refresh_now_playing(guild.id)
        else:
            # Show selection dropdown
            embed = discord.Embed(
                title="Search Results",
                description=f"Select a song from the results for: **{query}**",
                color=discord.Color.blurple()
            )
            view = SearchResultView(results, interaction.user, voice_channel)
            await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="loop", description="Loop the currently playing track.")
async def loop_track(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    song = guild_current_song.get(guild_id)

    if not song:
        return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    if guild_loop_mode.get(guild_id) == "track":
        return await interaction.response.send_message("Already looping the current track.", ephemeral=True)

    # Enable loop for current track
    guild_loop_mode[guild_id] = "track"
    guild_loop_song[guild_id] = song

    await interaction.response.send_message(f"Now looping: **{song.title}**")

    # Refresh Now Playing to keep it as most recent and update loop status
    await refresh_now_playing(guild_id)


@bot.tree.command(name="stoploop", description="Stop looping after current track finishes.")
async def stop_loop(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    if guild_loop_mode.get(guild_id) == "off":
        return await interaction.response.send_message("Loop is not active.", ephemeral=True)

    guild_loop_mode[guild_id] = "off"
    guild_loop_song[guild_id] = None

    await interaction.response.send_message("Loop disabled. Current track will finish, then queue continues.")

    # Refresh Now Playing to keep it as most recent and update loop status
    await refresh_now_playing(guild_id)


@bot.tree.command(name="skipcurrent", description="Skip the currently playing track (stops loop if active).")
async def skipcurrent(interaction: discord.Interaction):
    guild = interaction.guild
    guild_id = guild.id
    vc = guild.voice_client

    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    # Disable loop when skipping
    if guild_loop_mode.get(guild_id) == "track":
        guild_loop_mode[guild_id] = "off"
        guild_loop_song[guild_id] = None

    vc.stop()
    await interaction.response.send_message("Skipped current track.")


@bot.tree.command(name="pause", description="Pause the current track.")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    vc.pause()

    await interaction.response.send_message("Paused.")

    # Refresh Now Playing to keep it as most recent
    await refresh_now_playing(interaction.guild.id)


@bot.tree.command(name="resume", description="Resume playback.")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
    vc.resume()

    await interaction.response.send_message("Resumed.")

    # Refresh Now Playing to keep it as most recent
    await refresh_now_playing(interaction.guild.id)


@bot.tree.command(name="nowplaying", description="Show the currently playing track.")
async def now_playing(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    song = guild_current_song.get(guild_id)

    if not song:
        return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    vc = interaction.guild.voice_client
    is_paused = vc.is_paused() if vc else False
    loop_mode = guild_loop_mode.get(guild_id, "off")

    embed = create_now_playing_embed(song, loop_mode, is_paused)
    view = MusicControlView(guild_id, is_paused)

    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="queue", description="Show the current queue.")
async def show_queue(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild.id
    q = guild_queues.get(guild_id)

    embed = discord.Embed(title="Music Queue", color=discord.Color.blurple())

    # Current song
    current = guild_current_song.get(guild_id)
    if current:
        loop_status = " (Looping)" if guild_loop_mode.get(guild_id) == "track" else ""
        embed.add_field(
            name="Now Playing" + loop_status,
            value=f"[{current.title}]({current.webpage_url}) - {current.requester.display_name}",
            inline=False
        )

    if not q or q.empty():
        embed.add_field(name="Up Next", value="Queue is empty.", inline=False)
    else:
        items = list(q._queue)
        if items:
            lines = [f"`{idx+1}.` [{it.title}]({it.webpage_url}) - {it.requester.display_name}"
                     for idx, it in enumerate(items[:10])]
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
            if len(items) > 10:
                embed.set_footer(text=f"And {len(items) - 10} more...")
        else:
            embed.add_field(name="Up Next", value="Queue is empty.", inline=False)

    await interaction.followup.send(embed=embed)

    # Refresh Now Playing to keep it as most recent
    await refresh_now_playing(guild_id)


@bot.tree.command(name="clearqueue", description="Clear the queue.")
async def clear_queue(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    q = guild_queues.get(guild_id)
    if not q:
        return await interaction.response.send_message("Queue is already empty.", ephemeral=True)

    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
    await interaction.response.send_message("Queue cleared.")

    # Refresh Now Playing to keep it as most recent
    await refresh_now_playing(guild_id)


@bot.tree.command(name="autoplay", description="Toggle autoplay (auto-enqueue related tracks).")
async def autoplay_toggle(interaction: discord.Interaction):
    gid = interaction.guild.id
    await ensure_queue(gid)
    guild_autoplay[gid] = not guild_autoplay.get(gid, False)
    status = "ON" if guild_autoplay[gid] else "OFF"

    embed = discord.Embed(
        title="Autoplay",
        description=f"Autoplay is now **{status}**",
        color=discord.Color.green() if guild_autoplay[gid] else discord.Color.greyple()
    )
    await interaction.response.send_message(embed=embed)

    # Refresh Now Playing to keep it as most recent
    await refresh_now_playing(gid)


@bot.tree.command(name="leave", description="Make the bot leave the voice channel.")
async def leave(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    vc = interaction.guild.voice_client

    if not vc or not vc.is_connected():
        return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)

    # Cancel player task
    task = guild_players.get(guild_id)
    if task:
        task.cancel()

    # Clean up state
    guild_loop_mode[guild_id] = "off"
    guild_loop_song[guild_id] = None
    guild_current_song[guild_id] = None

    await vc.disconnect()
    await interaction.response.send_message("Left the voice channel.")


@bot.tree.command(name="about", description="About the bot.")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Music Bot",
        description="A feature-rich Discord music bot.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Sources", value="YouTube, SoundCloud, Spotify*, Apple Music*", inline=False)
    embed.add_field(name="Features", value="Queue, Loop, Autoplay, Rich Embeds, Button Controls", inline=False)
    embed.set_footer(text="*Spotify/Apple Music links are searched on YouTube")
    await interaction.response.send_message(embed=embed)

    # Refresh Now Playing to keep it as most recent
    await refresh_now_playing(interaction.guild.id)


@bot.tree.command(name="help", description="Show help for commands.")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Music Bot Commands",
        color=discord.Color.blurple()
    )

    commands_list = [
        ("`/play <query>`", "Search and play (shows selection for search terms)"),
        ("`/loop <link>`", "Add track to queue, loops when reached"),
        ("`/startloop`", "Start looping the current track"),
        ("`/stoploop`", "Stop loop after current track finishes"),
        ("`/skipcurrent`", "Skip current track (stops loop if active)"),
        ("`/pause`", "Pause playback"),
        ("`/resume`", "Resume playback"),
        ("`/nowplaying`", "Show current track with controls"),
        ("`/queue`", "Show the queue"),
        ("`/clearqueue`", "Clear the queue"),
        ("`/autoplay`", "Toggle autoplay on/off"),
        ("`/leave`", "Disconnect the bot"),
        ("`/about`", "Info about the bot"),
    ]

    embed.description = "\n".join([f"{cmd} - {desc}" for cmd, desc in commands_list])
    embed.set_footer(text="Supports: YouTube, SoundCloud, Spotify, Apple Music")

    await interaction.response.send_message(embed=embed)

    # Refresh Now Playing to keep it as most recent
    await refresh_now_playing(interaction.guild.id)


# Run the bot with reconnect enabled
if __name__ == "__main__":
    bot.run(TOKEN, reconnect=True)
