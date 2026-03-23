import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from audio import search_youtube
from embeds import create_error_embed, create_now_playing_embed, format_duration
from helpers import check_voice_permissions, refresh_now_playing
from models import Song, get_state
from player import cleanup_guild, player_loop
from ui import MusicControlView, SearchResultView

log = logging.getLogger("musicbot")


def register_commands(bot: commands.Bot):
    """Register all slash commands onto the bot. Called once from main.py."""

    @bot.tree.command(name="play", description="Play a song from YouTube/Spotify/Apple/SoundCloud (or search by name).")
    @app_commands.describe(query="Song name or URL")
    async def play(interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send(
                embed=create_error_embed("Not in Voice", "You must be in a voice channel to use /play.")
            )

        voice_channel = interaction.user.voice.channel
        guild = interaction.guild
        state = get_state(guild.id)
        state.text_channel = interaction.channel
        state.last_voice_channel = voice_channel

        is_url = bool(re.match(r"https?://", query))

        if is_url:
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
                thumbnail=info.get("thumbnail"),
            )

            vc: discord.VoiceClient = guild.voice_client
            if not vc or not vc.is_connected():
                err = await check_voice_permissions(voice_channel, guild)
                if err:
                    return await interaction.followup.send(embed=create_error_embed("Permission Error", err))
                try:
                    vc = await voice_channel.connect(timeout=10.0, reconnect=True)
                except asyncio.TimeoutError:
                    return await interaction.followup.send(
                        embed=create_error_embed("Timeout", "Failed to connect to voice channel.")
                    )
                except Exception as e:
                    return await interaction.followup.send(embed=create_error_embed("Connection Failed", str(e)))
            else:
                if vc.channel != voice_channel:
                    await vc.move_to(voice_channel)

            await state.queue.put(song)
            if state.player_task is None or state.player_task.done():
                state.player_task = bot.loop.create_task(player_loop(guild, vc))

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
            await refresh_now_playing(guild.id)

        else:
            # Text search — show dropdown for up to 5 results
            try:
                results = await search_youtube(query, max_results=5)
            except Exception as e:
                return await interaction.followup.send(embed=create_error_embed("Search Failed", str(e)))

            if not results:
                return await interaction.followup.send(
                    embed=create_error_embed("No Results", "No songs found for your search.")
                )

            if len(results) == 1:
                # Only one result — queue it directly
                info = results[0]
                song = Song(
                    title=info["title"],
                    webpage_url=info["webpage_url"],
                    requester=interaction.user,
                    duration=info.get("duration"),
                    thumbnail=info.get("thumbnail"),
                )

                vc: discord.VoiceClient = guild.voice_client
                if not vc or not vc.is_connected():
                    err = await check_voice_permissions(voice_channel, guild)
                    if err:
                        return await interaction.followup.send(embed=create_error_embed("Permission Error", err))
                    try:
                        vc = await voice_channel.connect(timeout=10.0, reconnect=True)
                    except Exception as e:
                        return await interaction.followup.send(embed=create_error_embed("Connection Failed", str(e)))

                await state.queue.put(song)
                if state.player_task is None or state.player_task.done():
                    state.player_task = bot.loop.create_task(player_loop(guild, vc))

                embed = discord.Embed(
                    title="Added to Queue",
                    description=f"[{song.title}]({song.webpage_url})",
                    color=discord.Color.green()
                )
                await interaction.followup.send(embed=embed)
                await refresh_now_playing(guild.id)
            else:
                embed = discord.Embed(
                    title="Search Results",
                    description=f"Select a song from the results for: **{query}**",
                    color=discord.Color.blurple()
                )
                view = SearchResultView(results, interaction.user, voice_channel)
                await interaction.followup.send(embed=embed, view=view)

    # ------------------------------------------------------------------ #

    @bot.tree.command(name="loop", description="Loop the currently playing track.")
    async def loop_track(interaction: discord.Interaction):
        guild_id = interaction.guild.id
        state = get_state(guild_id)

        if not state.current_song:
            return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        if state.loop_mode == "track":
            return await interaction.response.send_message("Already looping the current track.", ephemeral=True)

        state.loop_mode = "track"
        state.loop_song = state.current_song
        await interaction.response.send_message(f"Now looping: **{state.current_song.title}**")
        await refresh_now_playing(guild_id)

    @bot.tree.command(name="stoploop", description="Stop looping after the current track finishes.")
    async def stop_loop(interaction: discord.Interaction):
        guild_id = interaction.guild.id
        state = get_state(guild_id)

        if state.loop_mode == "off":
            return await interaction.response.send_message("Loop is not active.", ephemeral=True)

        state.loop_mode = "off"
        state.loop_song = None
        state.loop_inject = None
        await interaction.response.send_message("Loop disabled. Current track will finish, then queue continues.")
        await refresh_now_playing(guild_id)

    @bot.tree.command(name="skipcurrent", description="Skip the currently playing track (stops loop if active).")
    async def skipcurrent(interaction: discord.Interaction):
        guild = interaction.guild
        state = get_state(guild.id)
        vc = guild.voice_client

        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

        if state.loop_mode == "track":
            state.loop_mode = "off"
            state.loop_song = None
            state.loop_inject = None

        vc.stop()
        await interaction.response.send_message("Skipped current track.")

    @bot.tree.command(name="pause", description="Pause the current track.")
    async def pause(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        vc.pause()
        await interaction.response.send_message("Paused.")
        await refresh_now_playing(interaction.guild.id)

    @bot.tree.command(name="resume", description="Resume playback.")
    async def resume(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        vc.resume()
        await interaction.response.send_message("Resumed.")
        await refresh_now_playing(interaction.guild.id)

    @bot.tree.command(name="nowplaying", description="Show the currently playing track.")
    async def now_playing(interaction: discord.Interaction):
        guild_id = interaction.guild.id
        state = get_state(guild_id)

        if not state.current_song:
            return await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

        vc = interaction.guild.voice_client
        is_paused = vc.is_paused() if vc else False
        embed = create_now_playing_embed(state.current_song, state.loop_mode, is_paused)
        view = MusicControlView(guild_id, is_paused)
        await interaction.response.send_message(embed=embed, view=view)

    @bot.tree.command(name="queue", description="Show the current queue.")
    async def show_queue(interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = interaction.guild.id
        state = get_state(guild_id)

        embed = discord.Embed(title="Music Queue", color=discord.Color.blurple())

        if state.current_song:
            loop_status = " (Looping)" if state.loop_mode == "track" else ""
            embed.add_field(
                name=f"Now Playing{loop_status}",
                value=f"[{state.current_song.title}]({state.current_song.webpage_url}) — {state.current_song.requester.display_name}",
                inline=False
            )

        if state.queue.empty():
            embed.add_field(name="Up Next", value="Queue is empty.", inline=False)
        else:
            items = list(state.queue._queue)
            lines = [
                f"`{i+1}.` [{s.title}]({s.webpage_url}) — {s.requester.display_name}"
                for i, s in enumerate(items[:10])
            ]
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
            if len(items) > 10:
                embed.set_footer(text=f"And {len(items) - 10} more...")

        await interaction.followup.send(embed=embed)
        await refresh_now_playing(guild_id)

    @bot.tree.command(name="clearqueue", description="Clear the queue.")
    async def clear_queue(interaction: discord.Interaction):
        guild_id = interaction.guild.id
        state = get_state(guild_id)

        if state.queue.empty():
            return await interaction.response.send_message("Queue is already empty.", ephemeral=True)

        while not state.queue.empty():
            try:
                state.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        await interaction.response.send_message("Queue cleared.")
        await refresh_now_playing(guild_id)

    @bot.tree.command(name="autoplay", description="Toggle autoplay (auto-enqueue related tracks).")
    async def autoplay_toggle(interaction: discord.Interaction):
        guild_id = interaction.guild.id
        state = get_state(guild_id)
        state.autoplay = not state.autoplay
        status = "ON" if state.autoplay else "OFF"

        embed = discord.Embed(
            title="Autoplay",
            description=f"Autoplay is now **{status}**",
            color=discord.Color.green() if state.autoplay else discord.Color.greyple()
        )
        await interaction.response.send_message(embed=embed)
        await refresh_now_playing(guild_id)

    @bot.tree.command(name="leave", description="Make the bot leave the voice channel.")
    async def leave(interaction: discord.Interaction):
        guild_id = interaction.guild.id
        state = get_state(guild_id)
        vc = interaction.guild.voice_client

        if not vc or not vc.is_connected():
            return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)

        if state.player_task:
            state.player_task.cancel()

        state.loop_mode = "off"
        state.loop_song = None
        state.loop_inject = None
        state.current_song = None

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
        await refresh_now_playing(interaction.guild.id)

    @bot.tree.command(name="help", description="Show help for commands.")
    async def help_cmd(interaction: discord.Interaction):
        embed = discord.Embed(title="Music Bot Commands", color=discord.Color.blurple())
        commands_list = [
            ("`/play <query>`",  "Search and play (shows selection for search terms)"),
            ("`/loop`",          "Loop the currently playing track"),
            ("`/stoploop`",      "Stop loop after current track finishes"),
            ("`/skipcurrent`",   "Skip current track (stops loop if active)"),
            ("`/pause`",         "Pause playback"),
            ("`/resume`",        "Resume playback"),
            ("`/nowplaying`",    "Show current track with controls"),
            ("`/queue`",         "Show the queue"),
            ("`/clearqueue`",    "Clear the queue"),
            ("`/autoplay`",      "Toggle autoplay on/off"),
            ("`/leave`",         "Disconnect the bot"),
            ("`/about`",         "Info about the bot"),
        ]
        embed.description = "\n".join(f"{cmd} — {desc}" for cmd, desc in commands_list)
        embed.set_footer(text="Supports: YouTube, SoundCloud, Spotify, Apple Music")
        await interaction.response.send_message(embed=embed)
        await refresh_now_playing(interaction.guild.id)
