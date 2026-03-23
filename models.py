import asyncio
from dataclasses import dataclass, field
from typing import Optional

import discord


class Song:
    def __init__(
        self,
        title: str,
        webpage_url: str,
        requester: discord.Member,
        duration: Optional[int] = None,
        thumbnail: Optional[str] = None,
    ):
        self.title = title
        self.webpage_url = webpage_url
        self.requester = requester
        self.duration = duration
        self.thumbnail = thumbnail
        self.start_time: Optional[float] = None

    def __str__(self):
        return f"{self.title} ({self.webpage_url})"


@dataclass
class GuildState:
    """All per-guild mutable state in one place."""
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    player_task: Optional[asyncio.Task] = None
    autoplay: bool = False
    loop_mode: str = "off"             # "off" | "track"
    loop_song: Optional[Song] = None
    loop_inject: Optional[Song] = None # Injected at front of next iteration — avoids O(n) queue drain
    current_song: Optional[Song] = None
    now_playing_msg: Optional[discord.Message] = None
    text_channel: Optional[discord.TextChannel] = None
    last_voice_channel: Optional[discord.VoiceChannel] = None


# Single registry of all guild states
_guild_states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    """Get-or-create the GuildState for a guild."""
    if guild_id not in _guild_states:
        _guild_states[guild_id] = GuildState()
    return _guild_states[guild_id]
