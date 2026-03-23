import asyncio
import logging
import time
from typing import Optional

import discord

from audio import create_ffmpeg_source, get_stream_url
from config import (
    ALONE_TIMEOUT,
    INACTIVITY_TIMEOUT,
    RECONNECT_ATTEMPTS,
    RECONNECT_DELAY,
    ytdl,
)
from helpers import refresh_now_playing, send_error_to_channel, send_now_playing
from models import Song, get_state

log = logging.getLogger("musicbot")

# Injected at startup by main.py
bot = None


async def player_loop(guild: discord.Guild, voice_client: discord.VoiceClient):
    """
    Main playback loop for a guild.

    Responsibilities:
    - Dequeues songs (or consumes loop_inject for O(1) looping)
    - Pre-fetches the next song's stream URL in the background
    - Handles reconnection if the voice client drops
    - Manages loop and autoplay logic after each track finishes
    """
    guild_id = guild.id
    state = get_state(guild_id)
    log.info(f"Starting player loop for guild {guild.name} ({guild_id})")

    while True:
        # --- Dequeue ---
        try:
            if state.loop_inject:
                # Loop mode: consume the pre-built next-loop song without touching the queue
                song: Song = state.loop_inject
                state.loop_inject = None
            else:
                song: Song = await asyncio.wait_for(state.queue.get(), timeout=INACTIVITY_TIMEOUT)
        except asyncio.TimeoutError:
            log.info(f"Queue inactive for {INACTIVITY_TIMEOUT}s in guild {guild_id}, disconnecting...")
            await cleanup_guild(guild_id, voice_client)
            break
        except asyncio.CancelledError:
            await cleanup_guild(guild_id, voice_client, disconnect=False)
            break

        # --- Alone check ---
        if voice_client.is_connected() and voice_client.channel:
            members = [m for m in voice_client.channel.members if not m.bot]
            if not members:
                log.info(f"Bot is alone in voice channel, waiting {ALONE_TIMEOUT}s...")
                await asyncio.sleep(ALONE_TIMEOUT)
                members = [m for m in voice_client.channel.members if not m.bot]
                if not members:
                    log.info(f"Still alone after {ALONE_TIMEOUT}s, disconnecting...")
                    await cleanup_guild(guild_id, voice_client)
                    break

        # --- Reconnect if dropped ---
        voice_client = await ensure_voice_connection(guild, voice_client, song)
        if not voice_client:
            log.error(f"Could not establish voice connection for guild {guild_id}")
            continue

        # --- Fetch stream URL and create audio source ---
        # Single fetch — stream_url and header_str are reused for create_ffmpeg_source
        try:
            stream_url, info, header_str = await get_stream_url(song.webpage_url)
            if not song.thumbnail and info.get("thumbnail"):
                song.thumbnail = info["thumbnail"]
        except Exception as e:
            log.error(f"Failed to get stream URL: {e}")
            await send_error_to_channel(guild_id, "Stream Error", f"Could not load: {song.title}")
            state.queue.task_done()
            continue

        state.current_song = song
        song.start_time = time.time()

        await send_now_playing(guild_id, song)

        # --- Pre-fetch next song's stream URL in the background ---
        prefetch_task: Optional[asyncio.Task] = None
        if not state.queue.empty():
            try:
                next_song: Song = list(state.queue._queue)[0]
                prefetch_task = asyncio.create_task(get_stream_url(next_song.webpage_url))
            except Exception:
                pass  # Non-critical; will fetch normally when the time comes

        # --- Create audio source (Opus: lower CPU, better quality than PCM) ---
        try:
            source = await create_ffmpeg_source(stream_url, header_str)
        except Exception as e:
            log.error(f"Failed to create audio source: {e}")
            await send_error_to_channel(guild_id, "Playback Error", f"Failed to play: {song.title}")
            state.queue.task_done()
            continue

        # --- Play ---
        done_event = asyncio.Event()
        playback_error = [None]

        def after_play(err):
            if err:
                log.error(f"Player error: {err}")
                playback_error[0] = err
            bot.loop.call_soon_threadsafe(done_event.set)

        try:
            voice_client.play(source, after=after_play)
        except Exception as e:
            log.error(f"Failed to start playback: {e}")
            await send_error_to_channel(guild_id, "Playback Error", f"Failed to play: {song.title}")
            state.queue.task_done()
            continue

        await done_event.wait()

        # Cancel pre-fetch if the track ended before it completed (e.g. very short track)
        if prefetch_task and not prefetch_task.done():
            prefetch_task.cancel()

        # --- Retry on playback error ---
        if playback_error[0]:
            log.warning("Playback error occurred, attempting re-extraction...")
            try:
                stream_url, _, header_str = await get_stream_url(song.webpage_url)
                source = await create_ffmpeg_source(stream_url, header_str)
                done_event.clear()
                voice_client.play(source, after=after_play)
                await done_event.wait()
            except Exception as e:
                log.error(f"Re-extraction failed: {e}")
                await send_error_to_channel(guild_id, "Stream Failed", f"Skipping: {song.title}")

        # --- Loop: inject at front without draining the queue (O(1)) ---
        if state.loop_mode == "track" and state.loop_song:
            ls = state.loop_song
            state.loop_inject = Song(
                title=ls.title,
                webpage_url=ls.webpage_url,
                requester=ls.requester,
                duration=ls.duration,
                thumbnail=ls.thumbnail,
            )

        # --- Autoplay (only when not looping) ---
        elif state.autoplay:
            try:
                loop = asyncio.get_running_loop()
                info_rel = await loop.run_in_executor(
                    None,
                    lambda: ytdl.extract_info(f"ytsearch1:{song.title} related", download=False)
                )
                if "entries" in info_rel and info_rel["entries"]:
                    entry = info_rel["entries"][0]
                    if entry.get("webpage_url") and entry["webpage_url"] != song.webpage_url:
                        await state.queue.put(Song(
                            title=entry.get("title"),
                            webpage_url=entry["webpage_url"],
                            requester=song.requester,
                            duration=entry.get("duration"),
                            thumbnail=entry.get("thumbnail"),
                        ))
            except Exception:
                log.debug("Autoplay search failed; continuing.")

        state.queue.task_done()
        state.current_song = None


async def cleanup_guild(guild_id: int, voice_client: discord.VoiceClient, disconnect: bool = True):
    """Disconnect from voice and reset all playback state for a guild."""
    if disconnect and voice_client and voice_client.is_connected():
        await voice_client.disconnect()
    state = get_state(guild_id)
    state.player_task = None
    state.current_song = None
    state.loop_mode = "off"
    state.loop_song = None
    state.loop_inject = None


async def ensure_voice_connection(
    guild: discord.Guild,
    voice_client: discord.VoiceClient,
    song: Song,
) -> Optional[discord.VoiceClient]:
    """Re-establish a dropped voice connection with exponential backoff."""
    if voice_client and voice_client.is_connected():
        return voice_client

    log.warning(f"Voice client disconnected for guild {guild.id}, attempting reconnect...")
    state = get_state(guild.id)

    channel = state.last_voice_channel or (
        song.requester.voice.channel if song.requester.voice else None
    )
    if not channel:
        log.error("Cannot reconnect: no voice channel available")
        return None

    for attempt in range(RECONNECT_ATTEMPTS):
        try:
            if attempt > 0:
                delay = RECONNECT_DELAY * (2 ** attempt)
                log.info(f"Reconnect attempt {attempt + 1}, waiting {delay}s...")
                await asyncio.sleep(delay)
            voice_client = await channel.connect(timeout=10.0, reconnect=True)
            state.last_voice_channel = channel
            log.info(f"Reconnected to voice channel in guild {guild.id}")
            return voice_client
        except Exception as e:
            log.error(f"Reconnect attempt {attempt + 1} failed: {e}")

    return None
