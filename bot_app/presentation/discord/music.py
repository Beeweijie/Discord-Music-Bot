import asyncio
import logging
from bot_app.application.diagnostics import log_scope
from typing import List
import discord
from discord import app_commands
from discord.ext import commands
from bot_app.domain.music.policies import is_valid_url
from bot_app.presentation.discord.panels import InteractionContext

class Music(commands.Cog):
    def __init__(self, bot):
        from bot_app.bootstrap import create_music_service
        self.service = create_music_service(bot)

    async def cog_unload(self):
        await self.service.close()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        await self.service.on_voice_state_update(member, before, after)

    async def _run_slash(
        self,
        interaction: discord.Interaction,
        action: str,
        *args,
    ):
        """把 slash interaction 转成内部 ctx，并统一 defer 防止长任务超时。"""
        await interaction.response.defer()
        ctx = InteractionContext(interaction)
        await self.service.dispatch(action, ctx, *args)


    async def play_input_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """给 /play input 提供本地音乐和 YouTube 搜索候选。"""
        current = current.strip()
        choices = self.service._local_music_choices(current)

        if len(current) < 2 or is_valid_url(current):
            return [app_commands.Choice(name=c.name, value=c.value) for c in choices[:25]]

        try:
            with log_scope(interaction.guild_id):
                youtube_choices = await asyncio.wait_for(
                    self.service.run_media(self.service._search_youtube_choices, current),
                    timeout=2.5,
                )
            choices.extend(youtube_choices)
        except Exception as e:
            logging.getLogger(__name__).debug("Search autocomplete failed", exc_info=True, extra={"guild_id": interaction.guild_id})

        return [app_commands.Choice(name=c.name, value=c.value) for c in choices[:25]]


    @commands.command(name="join")
    async def join_prefix(self, ctx):
        await self.service.dispatch('join', ctx)


    @commands.command(name="play")
    async def play_prefix(self, ctx, *, input: str = ""):
        await self.service.dispatch('play', ctx, input)


    @commands.command(name="playlist_defult")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def playlist_defult(self, ctx, *, url: str):
        """Set this server default playlist: !playlist_defult <URL> (Manage Server required)."""
        management = self.service.bot.management
        settings = management.get_settings("music", ctx.guild.id)
        settings["default_playlist_url"] = url.strip().strip("<>")
        try:
            management.update_settings("music", settings, ctx.guild.id)
        except ValueError as error:
            await ctx.send(str(error))
            return
        await ctx.send("Default playlist saved for this server. Empty /play will use:" + settings["default_playlist_url"])

    @commands.command(name="seek")
    async def seek_prefix(self, ctx, position: str):
        await self.service.dispatch('seek', ctx, position)

    @app_commands.command(name="seek", description="Seek to a position in the current track")
    @app_commands.describe(position="Seconds or m:ss; +30 forward, -15 backward, 0 restart")
    async def seek_slash(self, interaction: discord.Interaction, position: str):
        await self._run_slash(interaction, 'seek', position)

    @commands.command(name="queue")
    async def queue_prefix(self, ctx, page: int = 1):
        await self.service.dispatch('queue', ctx, page)


    @commands.command(name="pause")
    async def pause_prefix(self, ctx):
        await self.service.dispatch('pause', ctx)


    @commands.command(name="resume")
    async def resume_prefix(self, ctx):
        await self.service.dispatch('resume', ctx)


    @commands.command(name="now")
    async def now_prefix(self, ctx):
        await self.service.dispatch('now', ctx)


    @commands.command(name="remove")
    async def remove_prefix(self, ctx, index: int):
        await self.service.dispatch('remove', ctx, index)


    @commands.command(name="volume")
    async def volume_prefix(self, ctx, volume: int):
        await self.service.dispatch('volume', ctx, volume)


    @commands.command(name="shuffle")
    async def shuffle_prefix(self, ctx):
        await self.service.dispatch('shuffle', ctx)


    @commands.command(name="skip")
    async def skip_prefix(self, ctx):
        await self.service.dispatch('skip', ctx)


    @commands.command(name="stop")
    async def stop_prefix(self, ctx):
        await self.service.dispatch('stop', ctx)


    @commands.command(name="help_music")
    async def help_music_prefix(self, ctx):
        await self.service.dispatch('help', ctx)


    @commands.command(name="clear")
    async def clear_prefix(self, ctx):
        await self.service.dispatch('clear', ctx)


    @commands.command(name="move")
    async def move_prefix(self, ctx, from_index: int, to_index: int):
        await self.service.dispatch('move', ctx, from_index, to_index)


    @commands.command(name="loop")
    async def loop_prefix(self, ctx, mode: str):
        await self.service.dispatch('loop', ctx, mode)


    @app_commands.command(name="join", description="Join your voice channel")
    async def join_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'join')


    @app_commands.command(name="play", description="Play a URL, playlist, local MP3, or search for a song")
    @app_commands.describe(input="URL or song name; leave empty for the default playlist")
    @app_commands.autocomplete(input=play_input_autocomplete)
    async def play_slash(self, interaction: discord.Interaction, input: str = ""):
        await self._run_slash(interaction, 'play', input)


    @app_commands.command(name="queue", description="View the queue in your voice channel")
    async def queue_slash(self, interaction: discord.Interaction, page: int = 1):
        await self._run_slash(interaction, 'queue', page)


    @app_commands.command(name="pause", description="Pause the current track")
    async def pause_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'pause')


    @app_commands.command(name="resume", description="Resume playback or a saved queue")
    async def resume_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'resume')


    @app_commands.command(name="now", description="Show the current track and player controls")
    async def now_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'now')


    @app_commands.command(name="remove", description="Remove a track by its queue position")
    @app_commands.describe(index="Queue position, starting at 1")
    async def remove_slash(self, interaction: discord.Interaction, index: int):
        await self._run_slash(interaction, 'remove', index)


    @app_commands.command(name="volume", description="Set playback volume (0-100)")
    @app_commands.describe(volume="Volume percentage (0-100)")
    async def volume_slash(self, interaction: discord.Interaction, volume: int):
        await self._run_slash(interaction, 'volume', volume)


    @app_commands.command(name="shuffle", description="Shuffle waiting tracks")
    async def shuffle_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'shuffle')


    @app_commands.command(name="skip", description="Skip the current track")
    async def skip_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'skip')


    @app_commands.command(name="stop", description="Stop, clear the queue, and disconnect")
    async def stop_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'stop')


    @app_commands.command(name="help_music", description="Show the music quick-start guide")
    async def help_music_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'help')


    @app_commands.command(name="clear", description="Clear waiting tracks without stopping playback")
    async def clear_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, 'clear')


    @app_commands.command(name="move", description="Move a track to another queue position")
    @app_commands.describe(from_index="Current position", to_index="New position")
    async def move_slash(
        self,
        interaction: discord.Interaction,
        from_index: int,
        to_index: int,
    ):
        await self._run_slash(interaction, 'move', from_index, to_index)


    @app_commands.command(name="loop", description="Choose a loop mode: off, track, or queue")
    @app_commands.describe(mode="off=disabled, one=track repeat, queue=queue repeat")
    async def loop_slash(self, interaction: discord.Interaction, mode: str):
        await self._run_slash(interaction, 'loop', mode)


    @commands.hybrid_command(name="replay", description="Restart the current track")
    @commands.guild_only()
    async def replay_command(self, ctx):
        """Restart the current track."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("replay", ctx)

    @commands.hybrid_command(name="fair", description="Arrange the queue to take turns by requester")
    @commands.guild_only()
    async def fair_command(self, ctx):
        """Arrange the queue to take turns by requester."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("fair", ctx)

    @commands.hybrid_command(name="dedupe", description="Remove duplicate waiting tracks")
    @commands.guild_only()
    async def dedupe_command(self, ctx):
        """Remove duplicate waiting tracks."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("dedupe", ctx)

    @commands.hybrid_command(name="nextup", description="Move a queued track to play next")
    @commands.guild_only()
    async def nextup_command(self, ctx, index: int):
        """Move a queued track to play next."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("nextup", ctx, index)

    @commands.hybrid_command(name="surprise", description="Pick a random queued track to play next")
    @commands.guild_only()
    async def surprise_command(self, ctx):
        """Pick a random queued track to play next."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("surprise", ctx)

    @commands.hybrid_command(name="sleep", description="Pause after a timer and keep the queue; 0 cancels")
    @commands.guild_only()
    async def sleep_command(self, ctx, minutes: int = 0):
        """Pause after a timer and keep the queue; 0 cancels."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("sleep", ctx, minutes)

    @commands.hybrid_command(name="history", description="View recently played tracks in this session")
    @commands.guild_only()
    async def history_command(self, ctx, page: int = 1):
        """View recently played tracks in this session."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("history", ctx, page)

    @commands.hybrid_command(name="favorite", description="Favorite the current track")
    @commands.guild_only()
    async def favorite_command(self, ctx):
        """Favorite the current track."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("favorite", ctx)

    @commands.hybrid_command(name="favorites", description="View your favorites in this server")
    @commands.guild_only()
    async def favorites_command(self, ctx, page: int = 1):
        """View your favorites in this server."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("favorites", ctx, page)

    @commands.hybrid_command(name="unfavorite", description="Remove a favorite by its position")
    @commands.guild_only()
    async def unfavorite_command(self, ctx, index: int):
        """Remove a favorite by its position."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("unfavorite", ctx, index)

    @commands.hybrid_command(name="playlist_save", description="Save the current track and queue as a personal playlist")
    @commands.guild_only()
    async def playlist_save_command(self, ctx, name: str):
        """Save the current track and queue as a personal playlist."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("playlist_save", ctx, name)

    @commands.hybrid_command(name="playlist_load", description="Queue a personal playlist; use favorites for your favorites")
    @commands.guild_only()
    async def playlist_load_command(self, ctx, name: str):
        """Queue a personal playlist; use favorites for your favorites."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("playlist_load", ctx, name)

    @commands.hybrid_command(name="playlists", description="List your personal playlists")
    @commands.guild_only()
    async def playlists_command(self, ctx):
        """List your personal playlists."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("playlists", ctx)

    @commands.hybrid_command(name="playlist_delete", description="Delete a personal playlist without affecting playback")
    @commands.guild_only()
    async def playlist_delete_command(self, ctx, name: str):
        """Delete a personal playlist without affecting playback."""
        await ctx.defer(ephemeral=True)
        await self.service.dispatch("playlist_delete", ctx, name)

    @playlist_load_command.autocomplete("name")
    @playlist_delete_command.autocomplete("name")
    async def playlist_name_choices(self, interaction, current):
        if not interaction.guild_id:
            return []
        try:
            data = self.service.library_repository.read(interaction.guild_id, interaction.user.id)
        except (OSError, ValueError):
            return []
        names = list(data["playlists"])
        if interaction.command.name == "playlist_load":
            names.insert(0, "favorites")
        return [app_commands.Choice(name=name, value=name) for name in names if current.casefold() in name.casefold()][:25]


async def setup(bot):
    await bot.add_cog(Music(bot))
