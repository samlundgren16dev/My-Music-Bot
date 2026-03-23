from typing import Optional

import discord

from models import Song


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


def create_error_embed(title: str, description: str) -> discord.Embed:
    """Create a user-friendly error embed."""
    return discord.Embed(
        title=f"Error: {title}",
        description=description,
        color=discord.Color.red()
    )


def create_now_playing_embed(song: Song, loop_mode: str = "off", is_paused: bool = False) -> discord.Embed:
    """Create a rich Now Playing embed."""
    embed = discord.Embed(
        title="Now Playing" if not is_paused else "Paused",
        description=f"[{song.title}]({song.webpage_url})",
        color=discord.Color.blurple() if not is_paused else discord.Color.orange()
    )

    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    embed.add_field(name="Duration", value=format_duration(song.duration), inline=True)
    embed.add_field(name="Requested by", value=song.requester.display_name, inline=True)
    embed.add_field(name="Loop", value="Off" if loop_mode == "off" else "Track", inline=True)

    return embed
