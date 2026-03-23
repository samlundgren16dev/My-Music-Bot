import asyncio
import logging

import discord

from audio import search_youtube
from embeds import create_error_embed, create_now_playing_embed, format_duration
from helpers import check_voice_permissions, refresh_now_playing
from models import Song, get_state

log = logging.getLogger("musicbot")

# Injected at startup by main.py
bot = None


class MusicControlView(discord.ui.View):
    """Persistent playback control buttons attached to the Now Playing embed."""

    def __init__(self, guild_id: int, is_paused: bool = False):
        super().__init__(timeout=None)
        self.guild_id = guild_id

        state = get_state(guild_id)
        for item in self.children:
            if hasattr(item, "custom_id"):
                if item.custom_id == "music_pause" and is_paused:
                    item.label = "Paused"
                    item.emoji = "▶️"
                    item.style = discord.ButtonStyle.success
                elif item.custom_id == "music_loop" and state.loop_mode == "track":
                    item.style = discord.ButtonStyle.success

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️", custom_id="music_pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("Not connected to voice.", ephemeral=True)

        state = get_state(self.guild_id)
        if vc.is_playing():
            vc.pause()
            button.label = "Paused"
            button.emoji = "▶️"
            button.style = discord.ButtonStyle.success
            if state.current_song:
                embed = create_now_playing_embed(state.current_song, state.loop_mode, is_paused=True)
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.response.edit_message(view=self)
        elif vc.is_paused():
            vc.resume()
            button.label = "Pause"
            button.emoji = "⏸️"
            button.style = discord.ButtonStyle.secondary
            if state.current_song:
                embed = create_now_playing_embed(state.current_song, state.loop_mode, is_paused=False)
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

        state = get_state(self.guild_id)
        state.loop_mode = "off"
        state.loop_song = None
        state.loop_inject = None
        vc.stop()
        await interaction.response.send_message("Skipped!", ephemeral=True)

    @discord.ui.button(label="Stop and Clear Queue", style=discord.ButtonStyle.danger, emoji="⏹️", custom_id="music_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)

        state = get_state(self.guild_id)
        while not state.queue.empty():
            try:
                state.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        state.loop_mode = "off"
        state.loop_song = None
        state.loop_inject = None
        vc.stop()
        await interaction.response.send_message("Stopped playback and cleared queue.", ephemeral=True)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="music_loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(self.guild_id)

        if state.loop_mode == "off":
            state.loop_mode = "track"
            state.loop_song = state.current_song
            button.style = discord.ButtonStyle.success
        else:
            state.loop_mode = "off"
            state.loop_song = None
            state.loop_inject = None
            button.style = discord.ButtonStyle.secondary

        if state.current_song:
            vc = interaction.guild.voice_client
            is_paused = vc.is_paused() if vc else False
            embed = create_now_playing_embed(state.current_song, state.loop_mode, is_paused)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            status = "Loop enabled!" if state.loop_mode == "track" else "Loop disabled."
            await interaction.response.send_message(status, ephemeral=True)


class SearchResultSelect(discord.ui.Select):
    """Dropdown menu populated with YouTube search results."""

    def __init__(self, results: list[dict], requester: discord.Member, voice_channel: discord.VoiceChannel):
        self.results = results
        self.requester = requester
        self.voice_channel = voice_channel

        options = [
            discord.SelectOption(
                label=result["title"][:100],
                description=f"Duration: {format_duration(result.get('duration'))}",
                value=str(i)
            )
            for i, result in enumerate(results[:5])
        ]

        super().__init__(placeholder="Choose a song...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        result = self.results[int(self.values[0])]
        guild = interaction.guild
        state = get_state(guild.id)

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
            thumbnail=info.get("thumbnail"),
        )

        vc: discord.VoiceClient = guild.voice_client
        if not vc or not vc.is_connected():
            err = await check_voice_permissions(self.voice_channel, guild)
            if err:
                return await interaction.followup.send(embed=create_error_embed("Permission Error", err))
            try:
                vc = await self.voice_channel.connect(timeout=10.0, reconnect=True)
            except Exception as e:
                return await interaction.followup.send(embed=create_error_embed("Connection Failed", str(e)))

        await state.queue.put(song)
        state.text_channel = interaction.channel
        state.last_voice_channel = self.voice_channel

        if state.player_task is None or state.player_task.done():
            from player import player_loop
            state.player_task = bot.loop.create_task(player_loop(guild, vc))

        embed = discord.Embed(
            title="Added to Queue",
            description=f"[{song.title}]({song.webpage_url})",
            color=discord.Color.green()
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        embed.add_field(name="Duration", value=format_duration(song.duration), inline=True)
        embed.add_field(name="Requested by", value=self.requester.display_name, inline=True)

        self.disabled = True
        await interaction.edit_original_response(embed=embed, view=self.view)
        await refresh_now_playing(guild.id)


class SearchResultView(discord.ui.View):
    """View wrapper around the search result dropdown."""

    def __init__(self, results: list[dict], requester: discord.Member, voice_channel: discord.VoiceChannel):
        super().__init__(timeout=60)
        self.add_item(SearchResultSelect(results, requester, voice_channel))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
