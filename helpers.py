import logging
from typing import Optional, TYPE_CHECKING

import discord

from embeds import create_error_embed, create_now_playing_embed
from models import Song, get_state

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger("musicbot")

# Injected at startup by main.py to avoid circular imports
bot: Optional["commands.Bot"] = None


async def check_voice_permissions(channel: discord.VoiceChannel, guild: discord.Guild) -> Optional[str]:
    """Return an error string if the bot lacks connect/speak permissions, else None."""
    perms = channel.permissions_for(guild.me)
    if not perms.connect:
        return "I don't have permission to join that voice channel."
    if not perms.speak:
        return "I don't have permission to speak in that voice channel."
    return None


async def send_now_playing(guild_id: int, song: Song):
    """Send a fresh Now Playing embed with control buttons."""
    # Import here to avoid circular dependency (ui imports helpers, helpers imports ui)
    from ui import MusicControlView

    state = get_state(guild_id)
    channel = state.text_channel
    if not channel:
        return

    guild = bot.get_guild(guild_id)
    vc = guild.voice_client if guild else None
    is_paused = vc.is_paused() if vc else False

    try:
        if state.now_playing_msg:
            try:
                await state.now_playing_msg.delete()
            except Exception:
                pass

        embed = create_now_playing_embed(song, state.loop_mode, is_paused)
        view = MusicControlView(guild_id, is_paused)
        state.now_playing_msg = await channel.send(embed=embed, view=view)
    except Exception as e:
        log.error(f"Failed to send Now Playing: {e}")


async def refresh_now_playing(guild_id: int):
    """Delete and resend the Now Playing embed so it stays at the bottom of chat."""
    from ui import MusicControlView

    state = get_state(guild_id)
    if not state.current_song or not state.text_channel:
        return

    guild = bot.get_guild(guild_id)
    vc = guild.voice_client if guild else None
    is_paused = vc.is_paused() if vc else False

    try:
        if state.now_playing_msg:
            try:
                await state.now_playing_msg.delete()
            except Exception:
                pass

        embed = create_now_playing_embed(state.current_song, state.loop_mode, is_paused)
        view = MusicControlView(guild_id, is_paused)
        state.now_playing_msg = await state.text_channel.send(embed=embed, view=view)
    except Exception as e:
        log.error(f"Failed to refresh Now Playing: {e}")


async def send_error_to_channel(guild_id: int, title: str, description: str):
    """Send an error embed to the guild's registered text channel."""
    state = get_state(guild_id)
    if state.text_channel:
        try:
            await state.text_channel.send(embed=create_error_embed(title, description))
        except Exception:
            pass
