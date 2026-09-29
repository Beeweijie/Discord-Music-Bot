import logging
from bot_app.application.diagnostics import session_logs
from urllib.parse import urlsplit, parse_qs
from typing import Optional
import discord
from bot_app.application.music.models import Song, ChannelSession

logger = logging.getLogger(__name__)

class InteractionContext:
    """给 slash command 用的轻量 ctx，让内部逻辑可以继续使用 ctx.send 等接口。"""

    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel

    async def send(self, *args, **kwargs):
        """根据 interaction 是否已响应，自动选择 response 或 followup。"""
        if self.interaction.response.is_done():
            await self.interaction.followup.send(*args, **kwargs)
        else:
            await self.interaction.response.send_message(*args, **kwargs)


class QueueView(discord.ui.View):
    """Live queue pages; only the reader can change their page."""
    def __init__(self, presenter, session_id, user_id, page):
        super().__init__(timeout=180)
        self.presenter, self.session_id, self.user_id, self.page = presenter, session_id, user_id, page

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Use /queue to open your own queue view.", ephemeral=True)
            return False
        return True

    async def change(self, interaction, delta):
        session = self.presenter.service.sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("This session has ended.", ephemeral=True)
            return
        self.page = max(1, min(max(1, (len(session.queue) + 9) // 10), self.page + delta))
        await interaction.response.edit_message(content=self.presenter.queue_text(session, self.page), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self.change(interaction, -1)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        await self.change(interaction, 0)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        await self.change(interaction, 1)


class AddSongModal(discord.ui.Modal, title="Add song"):
    query = discord.ui.TextInput(label="Song name or URL", placeholder="Song name or YouTube / Bilibili URL; empty uses the default playlist", required=False, max_length=1000)

    def __init__(self, music, session_id):
        super().__init__()
        self.music, self.session_id = music, session_id

    async def on_submit(self, interaction):
        channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if not channel or self.music._channel_key(channel) != self.session_id:
            await interaction.response.send_message("Join this player's voice channel first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.music.dispatch("play", InteractionContext(interaction), self.query.value)


class NowPlayingView(discord.ui.View):
    """Persistent controls shown under the now-playing panel."""

    def __init__(self, music: "Music", channel_id: str):
        super().__init__(timeout=None)
        self.music = music
        self.channel_id = channel_id
        session = music.sessions.get(channel_id)
        if session:
            for item in self.children:
                if getattr(item, "custom_id", "") == "music:pause" and (session.restored or session.sleeping or (session.vc and session.vc.is_paused())):
                    item.label = "Resume"
                elif getattr(item, "custom_id", "") == "music:loop":
                    item.label = {"off": "Loop: off", "one": "Loop: track", "queue": "Loop: queue"}[session.loop_mode]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        session = self.music.sessions.get(self.channel_id)
        if not session or session.stopped:
            await interaction.response.send_message("This player is no longer active. Use /now to open the current player.", ephemeral=True)
            return False
        if not channel or self.music._channel_key(channel) != self.channel_id:
            await interaction.response.send_message("Join this player's voice channel first.", ephemeral=True)
            return False
        return True

    async def _ctx(self, interaction: discord.Interaction) -> InteractionContext:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        return InteractionContext(interaction)

    async def _refresh_panel(self):
        session = self.music.sessions.get(self.channel_id)
        if session:
            await self.music._refresh_now_playing_panel(session)

    @discord.ui.button(label="Queue", custom_id="music:queue", style=discord.ButtonStyle.secondary, row=0)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music.dispatch('queue', await self._ctx(interaction))

    @discord.ui.button(label="Pause", custom_id="music:pause", style=discord.ButtonStyle.secondary, row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await self._ctx(interaction)
        session = self.music.sessions.get(self.channel_id)
        if session and (session.restored or session.sleeping or (session.vc and session.vc.is_paused())):
            await self.music.dispatch('resume', ctx)
        else:
            await self.music.dispatch('pause', ctx)
        await self._refresh_panel()

    @discord.ui.button(label="Skip", custom_id="music:skip", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music.dispatch('skip', await self._ctx(interaction))

    @discord.ui.button(label="Loop: off", custom_id="music:loop", style=discord.ButtonStyle.secondary, row=1)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await self._ctx(interaction)
        session = self.music.sessions.get(self.channel_id)
        if not session:
            await ctx.send("No active playback session.")
            return
        next_mode = {"off": "one", "one": "queue", "queue": "off"}.get(session.loop_mode, "one")
        await self.music.dispatch('loop', ctx, next_mode)
        await self._refresh_panel()

    @discord.ui.button(label="Stop & clear", custom_id="music:stop", style=discord.ButtonStyle.danger, row=2)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music.dispatch('stop', await self._ctx(interaction))

    @discord.ui.button(label="Replay", custom_id="music:replay", style=discord.ButtonStyle.secondary, row=1)
    async def replay_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music.dispatch('replay', await self._ctx(interaction))

    @discord.ui.button(label="Add song", custom_id="music:add", style=discord.ButtonStyle.primary, row=0)
    async def add_button(self, interaction, button):
        await interaction.response.send_modal(AddSongModal(self.music, self.channel_id))

    @discord.ui.button(label="Favorite", emoji="❤️", custom_id="music:favorite", style=discord.ButtonStyle.secondary, row=1)
    async def favorite_button(self, interaction, button):
        await self.music.dispatch("favorite", await self._ctx(interaction))

    @discord.ui.button(label="Surprise next", emoji="🎲", custom_id="music:surprise", style=discord.ButtonStyle.secondary, row=1)
    async def surprise_button(self, interaction, button):
        await self.music.dispatch("surprise", await self._ctx(interaction))

    async def on_error(self, interaction, error, item):
        logger.exception("Player button failed", exc_info=error, extra={"guild_id": interaction.guild_id})
        await InteractionContext(interaction).send("Operation failed. Please retry; details are in the log.", ephemeral=True)


class MusicPresenter:
    def __init__(self, bot):
        self.bot = bot
        self.service = None

    def queue_text(self, session, page):
        pages = max(1, (len(session.queue) + 9) // 10)
        if not 1 <= page <= pages:
            raise ValueError(f"Page must be between 1 and {pages}.")
        lines = [f"**Queue** · {len(session.queue)} tracks · Page {page}/{pages}"]
        if session.current_song:
            lines.append("Now playing:" + discord.utils.escape_markdown(session.current_song.title[:120]))
        start = (page - 1) * 10
        for i, song in enumerate(session.queue[start:start + 10], start + 1):
            title = discord.utils.escape_markdown(song.title)[:100]
            requester = discord.utils.escape_markdown(song.requester_name)[:30]
            lines.append(f"{i}. {title} — {requester}")
        if not session.queue:
            lines.append("Queue is empty. Click Add song or use /play.")
        lines.append("/nextup index: play next · /remove index: remove · /fair: take turns")
        return "\n".join(lines)

    async def send_queue(self, ctx, session, page):
        await ctx.send(self.queue_text(session, page), view=QueueView(self, self.service._session_status_key(session), ctx.author.id, page))

    def _queued_song_message(self, song: Song, queue_size: int) -> str:
        if queue_size <= 1:
            return f"✅ Queued: **{song.title}**"
        return f"✅ Queued: **{song.title}** (position {queue_size})"


    def _format_duration(self, seconds: Optional[int]) -> str:
        if seconds is None:
            return "--:--"
        seconds = int(seconds)
        minutes, second = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minute:02d}:{second:02d}"
        return f"{minute}:{second:02d}"


    def _build_now_playing_embed(self, session: ChannelSession) -> discord.Embed:
        song = session.current_song or session.preparing_song
        title = "Preparing" if session.preparing_song and not session.current_song else "Now playing" if song else "Music player"
        embed = discord.Embed(title=f"💿 {title}", color=discord.Color.blurple())

        if song:
            display_title = discord.utils.escape_markdown(song.title[:300])
            if song.webpage_url:
                embed.description = f"[{display_title}]({song.webpage_url})"
            else:
                embed.description = f"**{display_title}**"
            cover = self._cover_url(song)
            if cover:
                embed.set_image(url=cover)
            if song.uploader:
                embed.add_field(name="Source", value=song.uploader[:1024], inline=True)
            embed.add_field(name="Requested by", value=song.requester_name[:1024] or "-", inline=True)
            embed.add_field(name="Duration", value=self._format_duration(song.duration), inline=True)
        else:
            embed.description = "No track is playing."

        vc = session.vc
        if vc and vc.is_paused():
            state = "Paused"
        elif vc and vc.is_playing():
            state = "Playing"
        else:
            state = "Saved queue" if session.restored else "Idle"

        if session.queue:
            embed.add_field(name="Up next", value=discord.utils.escape_markdown(session.queue[0].title[:200]), inline=False)
        embed.set_footer(
            text=(
                f"{state} · Volume {session.volume}% · Queue {len(session.queue)} · "
                f"Loop { {'off': 'off', 'one': 'track', 'queue': 'queue'}[session.loop_mode]}"
            )
        )
        return embed


    def _cover_url(self, song):
        if song.thumbnail:
            parsed = urlsplit(song.thumbnail)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                return song.thumbnail
        parsed = urlsplit(song.webpage_url or song.input)
        host = (parsed.hostname or "").lower()
        video_id = ""
        if host == "youtube.com" or host.endswith(".youtube.com"):
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
        elif host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        if video_id and all(c.isascii() and (c.isalnum() or c in "_-") for c in video_id):
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        return None

    @session_logs
    async def _update_panel(self, session, text_channel=None, disabled_reason=None):
        # Build the latest song state inside the lock, so edits cannot arrive out of order.
        async with session.panel_lock:
            channel = self.bot.get_channel(session.panel_channel_id or session.last_text_channel_id) or text_channel
            if not channel:
                return
            embed = self._build_now_playing_embed(session)
            view = NowPlayingView(self.service, self.service._session_status_key(session))
            if disabled_reason:
                embed.title = "💿 " + disabled_reason
                for item in view.children:
                    item.disabled = True
            try:
                if session.panel_message_id:
                    try:
                        message = channel.get_partial_message(session.panel_message_id)
                        # Remove any GIF attachment left by the previous panel version.
                        await message.edit(embed=embed, view=view, attachments=[])
                        return
                    except discord.NotFound:
                        session.panel_message_id = None
                        session.panel_channel_id = None
                if disabled_reason:
                    return
                message = await channel.send(embed=embed, view=view)
                session.panel_channel_id = channel.id
                session.panel_message_id = message.id
                self.service._persist_session(session)
            except discord.Forbidden:
                logger.warning("没有权限更新音乐面板，频道 %s", channel.id)
            except discord.HTTPException:
                logger.warning("音乐面板更新失败，稍后重试", exc_info=True)

    @session_logs
    async def _refresh_now_playing_panel(self, session):
        try:
            await self._update_panel(session)
        except Exception:
            logger.exception("刷新面板失败；不影响播放，稍后重试")

    async def _send_or_update_now_playing_panel(self, text_channel, channel_id, session):
        try:
            await self._update_panel(session, text_channel)
        except Exception:
            logger.exception("更新面板失败；不影响播放，稍后重试")

    @session_logs
    async def _disable_now_playing_panel(self, session, reason="Stopped"):
        try:
            await self._update_panel(session, disabled_reason=reason)
        except Exception:
            logger.exception("禁用面板失败")

    async def _safe_send(self, channel, content=None, **kwargs):
        if not channel:
            return None
        try:
            return await channel.send(content, **kwargs)
        except discord.HTTPException:
            logger.warning("无法发送音乐消息到频道 %s", getattr(channel, "id", "unknown"))
            return None
