import asyncio
import logging
import re
import subprocess

import discord

from config import (
    RECONNECT_ATTEMPTS,
    RECONNECT_DELAY,
    ytdl,
    ytdl_search,
)

log = logging.getLogger("musicbot")


async def search_youtube(query: str, max_results: int = 1) -> list[dict]:
    """
    Use yt-dlp to search or extract info from the given query.
    Returns a list of result dicts. Handles YouTube, SoundCloud, Spotify, and Apple Music.
    """
    loop = asyncio.get_running_loop()
    to_search = query

    # Spotify / Apple Music — extract metadata then fall back to YouTube search
    if "open.spotify.com/track" in query or "spotify:track:" in query or "music.apple.com" in query:
        try:
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            artist = info.get("artist") or info.get("uploader") or ""
            title = info.get("title") or ""
            if title:
                search_term = f"{artist} - {title}".strip()
                to_search = f"ytsearch{max_results}:{search_term}"
        except Exception:
            to_search = f"ytsearch{max_results}:{query}"

    # SoundCloud — pass URL directly
    elif "soundcloud.com" in query:
        to_search = query

    # Any other direct URL — pass through
    elif re.match(r"https?://", query):
        to_search = query

    # Plain text — YouTube search
    else:
        to_search = f"ytsearch{max_results}:{query}"

    try:
        if max_results > 1 and not re.match(r"https?://", query):
            info = await loop.run_in_executor(None, lambda: ytdl_search.extract_info(to_search, download=False))
        else:
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(to_search, download=False))
    except Exception as e:
        log.exception("yt-dlp extract failed")
        raise RuntimeError(f"Failed to retrieve info: {e}") from e

    # Multiple results
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
    entry = info["entries"][0] if "entries" in info and info["entries"] else info
    return [{
        "title": entry.get("title", "Unknown title"),
        "webpage_url": entry.get("webpage_url") or entry.get("url"),
        "duration": entry.get("duration"),
        "thumbnail": entry.get("thumbnail"),
    }]


async def get_stream_url(webpage_url: str) -> tuple[str, dict, str]:
    """
    Extract a fresh streamable audio URL with exponential-backoff retry.
    Returns (stream_url, info, header_str) where header_str is a pre-built
    FFmpeg -headers argument string built from yt-dlp's http_headers.
    YouTube streams with rqh=1 require these headers or FFmpeg will fail to open them.
    """
    loop = asyncio.get_running_loop()
    for attempt in range(RECONNECT_ATTEMPTS):
        try:
            info = await loop.run_in_executor(None, lambda: ytdl.extract_info(webpage_url, download=False))
            stream_url = info.get("url") or webpage_url

            # FFmpeg -headers expects "Key: Value\r\n" pairs as a single string.
            # Double-quotes around the value ensure shlex.split keeps it as one token
            # on both Windows and Linux.
            headers = info.get("http_headers", {})
            header_str = ""
            if headers:
                raw = "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n"
                header_str = f' -headers "{raw}"'

            log.debug(f"Stream URL ext={info.get('ext')} acodec={info.get('acodec')} header_str={header_str!r}")
            return stream_url, info, header_str
        except Exception as e:
            log.warning(f"Stream extraction attempt {attempt + 1} failed: {e}")
            if attempt < RECONNECT_ATTEMPTS - 1:
                await asyncio.sleep(RECONNECT_DELAY * (attempt + 1))
    raise RuntimeError("Failed to extract stream URL after retries")


async def create_ffmpeg_source(url: str, header_str: str = "") -> discord.FFmpegOpusAudio:
    """
    Create an FFmpegOpusAudio source, forwarding yt-dlp's HTTP headers to FFmpeg.
    Required for YouTube streams that include rqh=1 (enforced Range request header).
    stderr is captured to a pipe so FFmpeg errors appear in the bot log.
    """
    before_options = (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
        f"{header_str}"
    )
    log.debug(f"FFmpeg before_options: {before_options!r}")
    log.debug(f"FFmpeg url: {url!r}")
    return await discord.FFmpegOpusAudio.from_probe(
        url,
        method="fallback",
        before_options=before_options,
        options="-vn",
        stderr=subprocess.PIPE,  # Capture FFmpeg stderr so errors show in bot log
    )
