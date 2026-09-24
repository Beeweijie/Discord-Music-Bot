"""音乐播放功能。

支持本地 mp3、YouTube / Bilibili 单曲、播放列表/合集、队列管理和预下载。
这个模块以语音频道为单位维护播放会话，避免不同语音频道的队列互相影响。
"""

import asyncio
import json
import logging
import os
import random
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from bot.path import CONFIG_DIR, MUSIC_DIR
from bot.status import drain_control_commands, remove_music_session, write_control_result, write_music_session, write_status


# ===== 基础配置 =====

DEFAULT_VOLUME = 40
MAX_QUEUE_SIZE = 200
MAX_PLAYLIST_SONGS = 100
PREDOWNLOAD_COUNT = 3
MAX_DOWNLOAD_CONCURRENCY = 2
CACHE_DELETE_DELAY_SECONDS = 5
CACHE_CLEANUP_INTERVAL_SECONDS = 15 * 60
CACHE_FILE_MAX_AGE_SECONDS = 60 * 60
VOICE_IDLE_TIMEOUT_SECONDS = 5 * 60
IDLE_CHECK_INTERVAL_SECONDS = 30
COMMAND_COOLDOWNS = {
    "play": 1.2,
    "remove": 1.0,
    "shuffle": 2.0,
    "skip": 1.5,
    "stop": 3.0,
}

logger = logging.getLogger(__name__)
NODE_JS_PATH = shutil.which("node")
BILIBILI_COOKIE_FILE = CONFIG_DIR / "bilibili_cookies.txt"
BILIBILI_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ===== URL 判断工具 =====

def is_valid_url(url: str) -> bool:
    """判断输入是否是 http/https 链接。"""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _has_host(url: str, domains) -> bool:
    if not is_valid_url(url):
        return False
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def is_youtube_url(url: str) -> bool:
    """判断输入是否是 YouTube 链接。"""
    return _has_host(url, ("youtube.com", "youtu.be"))


def is_bilibili_url(url: str) -> bool:
    """判断输入是否是 Bilibili 链接。"""
    return _has_host(url, ("bilibili.com", "b23.tv"))


# ===== 数据结构 =====

@dataclass
class Song:
    """队列中的单首歌曲。"""

    input: str
    title: str
    requester_id: int
    requester_name: str
    is_url: bool
    webpage_url: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    uploader: Optional[str] = None
    local_path: Optional[Path] = None
    downloaded: bool = False
    downloading: bool = False
    cancelled: bool = False
    suppress_repeat: bool = False
    download_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)


@dataclass
class ChannelSession:
    """一个语音频道对应一个播放会话。"""

    queue: List[Song] = field(default_factory=list)
    vc: Optional[discord.VoiceClient] = None
    last_text_channel_id: Optional[int] = None
    current_song: Optional[Song] = None
    preparing_song: Optional[Song] = None
    panel_channel_id: Optional[int] = None
    panel_message_id: Optional[int] = None
    predownload_task: Optional[asyncio.Task] = None
    idle_task: Optional[asyncio.Task] = None
    volume: int = DEFAULT_VOLUME
    guild_id: Optional[int] = None
    guild_name: Optional[str] = None
    channel_id: Optional[int] = None
    channel_name: Optional[str] = None
    last_command_at: float = field(default_factory=time.monotonic)
    empty_since: Optional[float] = None
    idle_since: Optional[float] = None
    loop_mode: str = "off"
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stopped: bool = False
    generation: int = 0


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


class NowPlayingView(discord.ui.View):
    """Persistent controls shown under the now-playing panel."""

    def __init__(self, music: "Music", channel_id: str):
        super().__init__(timeout=VOICE_IDLE_TIMEOUT_SECONDS * 2)
        self.music = music
        self.channel_id = channel_id
        session = music.sessions.get(channel_id)
        if session:
            for item in self.children:
                if getattr(item, "label", "") == "PAUSE" and session.vc and session.vc.is_paused():
                    item.label = "RESUME"
                elif getattr(item, "label", "") == "LOOP":
                    item.label = f"LOOP {session.loop_mode.upper()}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        session = self.music.sessions.get(self.channel_id)
        if not session or session.stopped:
            await interaction.response.send_message("这个播放面板已失效，请用 /now 查看当前播放。", ephemeral=True)
            return False
        if not channel or self.music._channel_key(channel) != self.channel_id:
            await interaction.response.send_message("请先加入这个播放面板对应的语音频道。", ephemeral=True)
            return False
        return True

    async def _ctx(self, interaction: discord.Interaction) -> InteractionContext:
        if not interaction.response.is_done():
            await interaction.response.defer()
        return InteractionContext(interaction)

    async def _refresh_panel(self):
        session = self.music.sessions.get(self.channel_id)
        if session:
            await self.music._refresh_now_playing_panel(session)

    @discord.ui.button(label="QUEUE", style=discord.ButtonStyle.secondary, row=0)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music._queue_impl(await self._ctx(interaction))

    @discord.ui.button(label="PAUSE", style=discord.ButtonStyle.secondary, row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await self._ctx(interaction)
        session = self.music.sessions.get(self.channel_id)
        if session and session.vc and session.vc.is_paused():
            await self.music._resume_impl(ctx)
        else:
            await self.music._pause_impl(ctx)
        await self._refresh_panel()

    @discord.ui.button(label="SKIP", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music._skip_impl(await self._ctx(interaction))

    @discord.ui.button(label="LOOP", style=discord.ButtonStyle.secondary, row=1)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await self._ctx(interaction)
        session = self.music.sessions.get(self.channel_id)
        if not session:
            await ctx.send("当前没有播放会话喵~")
            return
        next_mode = {"off": "one", "one": "queue", "queue": "off"}.get(session.loop_mode, "one")
        await self.music._loop_impl(ctx, next_mode)
        await self._refresh_panel()

    @discord.ui.button(label="STOP", style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music._stop_impl(await self._ctx(interaction))

    @discord.ui.button(label="REPLAY", style=discord.ButtonStyle.secondary, row=1)
    async def replay_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music._replay_impl(await self._ctx(interaction))


class Music(commands.Cog):
    """音乐命令与播放调度。"""

    def __init__(self, bot):
        self.bot = bot
        self.sessions: Dict[str, ChannelSession] = {}
        self.search_cache = {}
        self.command_cooldowns: Dict[tuple, float] = {}
        self.download_semaphore = asyncio.Semaphore(MAX_DOWNLOAD_CONCURRENCY)
        self.connection_locks = {}
        self.background_tasks = set()
        self.active_downloads = set()
        self.closing = False

        self.cache_dir = Path(MUSIC_DIR) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_cache_dir()

        fallback = Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe")
        self.ffmpeg_path = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or (
            str(fallback) if os.name == "nt" and fallback.is_file() else "ffmpeg"
        )

        self.cache_cleanup_task = self.bot.loop.create_task(self._cache_cleanup_loop())
        self.control_task = self.bot.loop.create_task(self._control_loop())

    async def cog_unload(self):
        """卸载时释放语音、播放回调和后台任务。"""
        self.closing = True
        if self.cache_cleanup_task and not self.cache_cleanup_task.done():
            self.cache_cleanup_task.cancel()
        if self.control_task and not self.control_task.done():
            self.control_task.cancel()
        for key in list(self.sessions):
            await self._dispose_session(key)
        await asyncio.gather(self.cache_cleanup_task, self.control_task, return_exceptions=True)

    def _spawn(self, coroutine):
        task = self.bot.loop.create_task(coroutine)
        self.background_tasks.add(task)
        def finished(done):
            self.background_tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.warning("音乐后台任务失败", exc_info=done.exception())
        task.add_done_callback(finished)
        return task

    # ===== slash command 适配 =====

    async def _run_slash(
        self,
        interaction: discord.Interaction,
        handler: Callable[..., Awaitable[None]],
        *args,
    ):
        """把 slash interaction 转成内部 ctx，并统一 defer 防止长任务超时。"""
        await interaction.response.defer()
        ctx = InteractionContext(interaction)
        await handler(ctx, *args)

    async def play_input_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """给 /play input 提供本地音乐和 YouTube 搜索候选。"""
        current = current.strip()
        choices = self._local_music_choices(current)

        if len(current) < 2 or is_valid_url(current):
            return choices[:25]

        try:
            youtube_choices = await asyncio.wait_for(
                self.bot.loop.run_in_executor(
                    None,
                    self._search_youtube_choices,
                    current,
                ),
                timeout=2.5,
            )
            choices.extend(youtube_choices)
        except Exception as e:
            print(f"播放搜索自动补全失败: {current} | {e}")

        return choices[:25]

    # ===== 会话与频道工具 =====

    def _get_user_voice_channel(self, ctx) -> Optional[discord.VoiceChannel]:
        """获取命令发送者当前所在的语音频道。"""
        if not getattr(ctx, "guild", None) or not getattr(ctx.author, "voice", None):
            return None
        return ctx.author.voice.channel

    def _session_key(self, guild_id: int, channel_id: int) -> str:
        """使用服务器和频道 ID 隔离会话。"""
        return f"{guild_id}:{channel_id}"

    def _channel_key(self, channel: discord.VoiceChannel) -> str:
        return self._session_key(channel.guild.id, channel.id)

    def _session_status_key(self, session: ChannelSession) -> str:
        if session.guild_id and session.channel_id:
            return self._session_key(session.guild_id, session.channel_id)
        return str(session.channel_id or "unknown")

    def _get_session(self, channel: discord.VoiceChannel) -> ChannelSession:
        """获取或创建语音频道的会话。"""
        key = self._channel_key(channel)
        if key not in self.sessions:
            self.sessions[key] = ChannelSession()
        return self.sessions[key]

    def _get_existing_session(self, ctx) -> Optional[ChannelSession]:
        """根据用户当前语音频道取已有会话。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            return None
        return self.sessions.get(self._channel_key(channel))

    def _cleanup_dead_vc(self, session: ChannelSession):
        """清理已经断开的 VoiceClient 引用。"""
        if session.vc and not session.vc.is_connected():
            session.vc = None

    def _get_text_channel(self, session: ChannelSession):
        """取回最近一次发起音乐命令的文字频道。"""
        if session.last_text_channel_id:
            return self.bot.get_channel(session.last_text_channel_id)
        return None

    def _get_context_ids(self, ctx, channel=None):
        """返回 guild/channel/user 标识，用于多服务器隔离 cooldown 和日志。"""
        guild_id = ctx.guild.id if ctx.guild else 0
        channel_id = channel.id if channel else 0
        user_id = ctx.author.id if ctx.author else 0
        return guild_id, channel_id, user_id

    async def _check_cooldown(
        self,
        ctx,
        command_name: str,
        seconds: float,
        channel=None,
        per_user: bool = False,
    ) -> bool:
        """按服务器和语音频道限制高频命令，避免一个服务器影响另一个服务器。"""
        guild_id, channel_id, user_id = self._get_context_ids(ctx, channel)
        cooldown_user = user_id if per_user else 0
        key = (guild_id, channel_id, command_name, cooldown_user)
        now = time.monotonic()
        if len(self.command_cooldowns) > 1000:
            self.command_cooldowns = {
                item_key: item_ready_at
                for item_key, item_ready_at in self.command_cooldowns.items()
                if item_ready_at > now
            }
        ready_at = self.command_cooldowns.get(key, 0)

        if now < ready_at:
            wait_time = max(0.1, ready_at - now)
            await ctx.send(f"操作太快啦喵~ 请再等 {wait_time:.1f} 秒")
            return False

        self.command_cooldowns[key] = now + seconds
        return True

    def _log_session(self, channel_id: int, message: str, **extra):
        """写入带频道/session 信息的调试日志，方便排查多服务器播放问题。"""
        detail = " ".join(f"{key}={value}" for key, value in extra.items())
        if detail:
            logger.info("[music channel=%s] %s | %s", channel_id, message, detail)
        else:
            logger.info("[music channel=%s] %s", channel_id, message)

    def _remember_session_location(self, session: ChannelSession, ctx=None, channel=None):
        """记录 session 所属服务器和语音频道，供托盘 UI 按服务器展示。"""
        if ctx and ctx.guild:
            session.guild_id = ctx.guild.id
            session.guild_name = ctx.guild.name
        if channel:
            session.channel_id = channel.id
            session.channel_name = channel.name
        elif session.vc and session.vc.channel:
            session.channel_id = session.vc.channel.id
            session.channel_name = session.vc.channel.name
            if session.vc.guild:
                session.guild_id = session.vc.guild.id
                session.guild_name = session.vc.guild.name

    def _write_music_status(self, session: Optional[ChannelSession] = None):
        """把当前音乐状态写给托盘小程序读取。"""
        current = session.current_song if session else None
        if session:
            self._remember_session_location(session)
            human_count = self._human_voice_count(session)
            session_id = self._session_status_key(session)
            write_music_session(
                session_id,
                {
                    "guild_id": session.guild_id,
                    "guild_name": session.guild_name or "-",
                    "channel_id": session.channel_id,
                    "channel_name": session.channel_name or "-",
                    "current_song": current.title if current else None,
                    "requester": current.requester_name if current else None,
                    "queue_size": len(session.queue),
                    "volume": session.volume,
                    "is_connected": bool(session.vc and session.vc.is_connected()),
                    "is_playing": bool(session.vc and session.vc.is_playing()),
                    "is_paused": bool(session.vc and session.vc.is_paused()),
                    "human_count": human_count,
                    "loop_mode": session.loop_mode,
                },
            )
        write_status(
            current_song=current.title if current else None,
            requester=current.requester_name if current else None,
            queue_size=len(session.queue) if session else 0,
            volume=session.volume if session else DEFAULT_VOLUME,
        )

    def _touch_session(self, session: ChannelSession, ctx=None, channel=None):
        """记录这个语音频道最近有人使用过音乐指令。"""
        self._remember_session_location(session, ctx, channel)
        session.last_command_at = time.monotonic()

    def _human_voice_count(self, session: ChannelSession) -> int:
        """统计当前语音频道里的真人数量，不把 bot 算进去。"""
        voice_channel = None
        if session.vc and session.vc.channel:
            voice_channel = session.vc.channel
        elif session.channel_id:
            voice_channel = self.bot.get_channel(session.channel_id)

        if not voice_channel or not hasattr(voice_channel, "members"):
            return 0
        return sum(1 for member in voice_channel.members if not member.bot)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Start the empty-channel timer as soon as the last human leaves."""
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel and (not after.channel or after.channel.id != before.channel.id):
                old_key = self._channel_key(before.channel)
                session = self.sessions.get(old_key)
                if session and not session.stopped:
                    await self._dispose_session(old_key, disconnect=after.channel is None)
            return
        if member.bot:
            return

        changed_channel_ids = {
            channel.id
            for channel in (before.channel, after.channel)
            if channel is not None
        }
        if not changed_channel_ids:
            return

        now = time.monotonic()
        for session in self.sessions.values():
            if not session.channel_id or session.channel_id not in changed_channel_ids:
                continue
            if self._human_voice_count(session) == 0:
                if session.empty_since is None:
                    session.empty_since = now
            else:
                session.empty_since = None

    def _start_idle_task(self, channel_id: int):
        """为当前语音频道启动自动离开检查。"""
        session = self.sessions.get(channel_id)
        if not session:
            return
        if session.idle_task and not session.idle_task.done():
            return
        session.idle_task = self.bot.loop.create_task(self._idle_watch_loop(channel_id))

    async def _idle_watch_loop(self, channel_id: int):
        """5 分钟无人或空闲后自动离开语音频道。"""
        while True:
            await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
            session = self.sessions.get(channel_id)
            if not session or session.stopped:
                return

            vc = session.vc
            if not vc or not vc.is_connected():
                return

            now = time.monotonic()
            human_count = self._human_voice_count(session)
            if human_count == 0:
                if session.empty_since is None:
                    session.empty_since = now
                elif now - session.empty_since >= VOICE_IDLE_TIMEOUT_SECONDS:
                    await self._auto_disconnect_session(
                        channel_id,
                        "语音频道已经 5 分钟没有其他人，我先退出啦~",
                    )
                    return
            else:
                session.empty_since = None

            is_playing = vc.is_playing() or vc.is_paused()
            has_queue = bool(session.queue or session.current_song or session.preparing_song)
            if is_playing or has_queue:
                session.idle_since = None
            elif session.idle_since is None:
                session.idle_since = now
            elif now - session.idle_since >= VOICE_IDLE_TIMEOUT_SECONDS:
                await self._auto_disconnect_session(
                    channel_id,
                    "播放结束后已空闲 5 分钟，我先退出啦~",
                )
                return

            self._write_music_status(session)

    async def _auto_disconnect_session(self, channel_id: int, reason: str):
        """自动断开一个 session，并清理队列、任务和缓存。"""
        session = self.sessions.get(channel_id)
        if not session:
            return
        text_channel = self._get_text_channel(session)
        await self._dispose_session(channel_id)
        if text_channel:
            await self._safe_send(text_channel, f"👋 {reason}")

    async def _dispose_session(self, channel_id: str, disconnect: bool = True):
        session = self.sessions.pop(channel_id, None)
        if not session:
            return
        session.stopped = True
        session.generation += 1
        async with session.queue_lock:
            for song in session.queue:
                self._cancel_song(song)
            session.queue.clear()
        if session.preparing_song:
            self._cancel_song(session.preparing_song)
            session.preparing_song = None
        if session.current_song:
            self._cancel_song(session.current_song)
            session.current_song = None
        tasks = [session.predownload_task, session.idle_task]
        for task in tasks:
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        vc = session.vc
        await self._disable_now_playing_panel(session)
        try:
            if vc:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                if disconnect:
                    await vc.disconnect(force=True)
        except Exception:
            logger.exception("断开语音会话失败: %s", channel_id)
        finally:
            session.vc = None
            remove_music_session(str(channel_id))
            await asyncio.gather(*(task for task in tasks if task and task is not asyncio.current_task()), return_exceptions=True)

    async def _retire_guild_sessions(self, guild_id: int, keep_channel_id: int):
        """同一服务器切换语音频道时，清理旧频道 session，避免旧回调继续调度。"""
        for session_key, session in list(self.sessions.items()):
            if session.channel_id == keep_channel_id:
                continue

            vc_guild_id = session.guild_id
            if vc_guild_id != guild_id:
                continue

            await self._dispose_session(session_key, disconnect=False)
            self._log_session(session.channel_id or 0, "retired stale guild session", guild_id=guild_id)

    def _delete_file_safely(self, file_path: Optional[Path]):
        """删除缓存文件，失败时只打印日志，不影响播放流程。"""
        if not file_path or not self._is_cache_path(file_path) or self._cache_file_in_use(file_path):
            return
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception as e:
            print(f"删除缓存文件失败: {file_path} | {e}")

    async def _delete_file_later(self, file_path: Optional[Path]):
        """延迟删除缓存文件，给 FFmpeg 一点释放文件句柄的时间。"""
        if not file_path or not self._is_cache_path(file_path):
            return

        for _ in range(6):
            await asyncio.sleep(CACHE_DELETE_DELAY_SECONDS)
            if self._cache_file_in_use(file_path):
                return
            try:
                if not file_path.exists():
                    return
                file_path.unlink()
                return
            except PermissionError:
                continue
            except Exception as e:
                print(f"删除缓存文件失败: {file_path} | {e}")
                return

        self._delete_file_safely(file_path)

    def _is_cache_path(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.cache_dir.resolve())
        except (OSError, ValueError):
            return False

    def _cache_file_in_use(self, path: Path) -> bool:
        if any(path.name.startswith(prefix + ".") for prefix in self.active_downloads.copy()):
            return True
        for session in list(self.sessions.values()):
            for song in [session.current_song, session.preparing_song, *session.queue]:
                if song and not song.cancelled and song.local_path == path:
                    return True
        return False

    async def _safe_send(self, channel, content=None, **kwargs):
        if not channel:
            return None
        try:
            return await channel.send(content, **kwargs)
        except discord.HTTPException:
            logger.warning("无法发送音乐消息到频道 %s", getattr(channel, "id", "unknown"))
            return None

    def _cleanup_cache_dir(self):
        """Bot 启动时清空远程音频缓存，避免缓存文件无限增长。"""
        for file_path in self.cache_dir.iterdir():
            if file_path.is_file():
                self._delete_file_safely(file_path)

    async def _cache_cleanup_loop(self):
        """定期清理过期缓存文件，处理异常退出后遗留的下载文件。"""
        while True:
            try:
                await asyncio.sleep(CACHE_CLEANUP_INTERVAL_SECONDS)
                cutoff = time.time() - CACHE_FILE_MAX_AGE_SECONDS

                for file_path in self.cache_dir.iterdir():
                    if not file_path.is_file():
                        continue
                    try:
                        if file_path.stat().st_mtime <= cutoff:
                            self._delete_file_safely(file_path)
                    except FileNotFoundError:
                        continue
                    except Exception as e:
                        logger.warning("清理缓存文件失败: %s | %s", file_path, e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("缓存清理任务异常: %s", e)

    async def _control_loop(self):
        """逐条领取控制命令，单个错误不阻塞后续操作。"""
        while True:
            try:
                await asyncio.sleep(1)
                for command in drain_control_commands():
                    try:
                        await self._handle_ui_control(command)
                    except Exception as error:
                        logger.warning("控制命令失败: %s", error)
                        write_control_result(command, False, str(error))
                    else:
                        write_control_result(command, True, "操作已完成")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("读取控制命令失败")

    async def _handle_ui_control(self, command: dict):
        session_id = command.get("session_id")
        action = command.get("action")
        session = self.sessions.get(session_id)
        if not session or session.stopped:
            raise ValueError("播放会话已结束，请刷新控制台")
        self._touch_session(session)
        vc = session.vc
        if action == "skip":
            await self._skip_session(session_id, session)
        elif action == "stop":
            await self._auto_disconnect_session(session_id, "已从控制台停止播放。")
            return
        elif action == "clear":
            async with session.queue_lock:
                for song in session.queue:
                    self._cancel_song(song)
                session.queue.clear()
        elif action == "volume":
            volume = int(command.get("volume", DEFAULT_VOLUME))
            if not 0 <= volume <= 100:
                raise ValueError("音量必须在 0 到 100 之间")
            self._set_volume(session, volume)
        elif action == "loop":
            mode = str(command.get("mode") or "off").lower()
            if mode not in {"off", "one", "queue"}:
                raise ValueError("无效的循环模式")
            session.loop_mode = mode
        elif action == "pause":
            if vc and vc.is_playing():
                vc.pause()
            else:
                raise ValueError("当前没有正在播放的歌曲")
        elif action == "resume":
            if vc and vc.is_paused():
                vc.resume()
            else:
                raise ValueError("当前没有暂停的歌曲")
        else:
            raise ValueError("不支持的控制命令")
        self._write_music_status(session)
        await self._refresh_now_playing_panel(session)

    def _song_key(self, song: Song) -> str:
        """生成歌曲去重 key，优先用输入链接，其次用标题。"""
        return (song.input or song.title).strip().lower()

    def _dedupe_songs(self, songs: List[Song]) -> List[Song]:
        """按链接/输入去重，保留第一次出现的歌曲。"""
        seen = set()
        unique_songs = []

        for song in songs:
            key = self._song_key(song)
            if not key or key in seen:
                continue
            seen.add(key)
            unique_songs.append(song)

        return unique_songs

    def _cancel_song(self, song: Song):
        """标记歌曲已取消，并安排清理它的缓存文件。"""
        song.cancelled = True
        if song.local_path:
            self._spawn(self._delete_file_later(song.local_path))
            song.local_path = None
            song.downloaded = False

    # ===== yt-dlp 解析与下载 =====

    def _normalize_youtube_playlist_url(self, url: str) -> str:
        """把 watch?v=xxx&list=yyy 形式统一成标准 playlist URL。"""
        match = re.search(r"list=([A-Za-z0-9_\-]+)", url)
        if match:
            list_id = match.group(1)
            return f"https://www.youtube.com/playlist?list={list_id}"
        return url

    def _normalize_youtube_watch_url(self, url: str) -> str:
        """把 YouTube radio/mix 链接转成普通 watch?v=... 链接。"""
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        video_ids = query.get("v")
        if not video_ids:
            return url

        new_query = urlencode({"v": video_ids[0]})
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_query, ""))

    def _compact_youtube_video_url(self, url: str, video_id: Optional[str] = None) -> Optional[str]:
        """把 YouTube 搜索结果压成不会超过 autocomplete 限制的 watch URL。"""
        candidate = str(url or "").strip()
        parsed = urlparse(candidate)
        host = parsed.netloc.lower()

        if not video_id:
            if "youtu.be" in host:
                video_id = parsed.path.strip("/").split("/")[0]
            elif "youtube.com" in host:
                query = parse_qs(parsed.query)
                video_id = (query.get("v") or [""])[0]
                if not video_id:
                    parts = [part for part in parsed.path.split("/") if part]
                    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
                        video_id = parts[1]
            elif candidate and not candidate.startswith("http"):
                video_id = candidate

        if not video_id:
            return None
        return f"https://www.youtube.com/watch?v={video_id}"

    def _youtube_search_entry_url(self, entry: dict) -> Optional[str]:
        """从 yt-dlp 搜索结果里拿可播放的 YouTube 链接。"""
        entry_id = entry.get("id")
        entry_url = (
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
            or entry_id
        )
        raw_url = str(entry_url or "")
        if raw_url.startswith("http") and not is_youtube_url(raw_url):
            return None
        compact_url = self._compact_youtube_video_url(raw_url, str(entry_id or ""))
        if compact_url:
            return compact_url
        if raw_url and is_youtube_url(raw_url):
            return raw_url
        return None

    def _is_youtube_radio_url(self, url: str) -> bool:
        """判断是否是 YouTube 自动生成的 radio/mix 链接。"""
        if not is_youtube_url(url):
            return False

        query = parse_qs(urlparse(url).query)
        list_id = (query.get("list") or [""])[0]
        return bool(query.get("v")) and (
            query.get("start_radio") == ["1"]
            or list_id.startswith("RD")
        )

    def _is_youtube_playlist_like_url(self, url: str) -> bool:
        """判断 /play 输入是否是普通 YouTube 播放列表。"""
        if not is_youtube_url(url) or "list=" not in url:
            return False
        return not self._is_youtube_radio_url(url)

    def _build_ydl_opts(
        self,
        output_template: Optional[str] = None,
        allow_playlist: bool = False,
        extract_flat: bool = False,
    ) -> dict:
        """构造 yt-dlp 参数；下载时会额外配置音频转码。"""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "ffmpeg_location": self.ffmpeg_path,
        }

        if NODE_JS_PATH:
            opts["js_runtimes"] = {"node": {"path": NODE_JS_PATH}}

        if not allow_playlist:
            opts["noplaylist"] = True

        if extract_flat:
            opts["extract_flat"] = "in_playlist"

        if output_template is not None:
            opts.update({
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "restrictfilenames": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "128",
                    }
                ],
            })

        return opts

    def _apply_site_opts(self, url: str, ydl_opts: dict) -> dict:
        """Add site-specific yt-dlp options without affecting other platforms."""
        if is_bilibili_url(url):
            headers = dict(ydl_opts.get("http_headers") or {})
            headers.update(BILIBILI_HTTP_HEADERS)
            ydl_opts["http_headers"] = headers
            if BILIBILI_COOKIE_FILE.exists():
                ydl_opts["cookiefile"] = str(BILIBILI_COOKIE_FILE)
        return ydl_opts

    def _clean_song_title(
        self,
        title: Optional[str],
        source_url: str = "",
        fallback: str = "未知标题",
    ) -> str:
        """Normalize noisy extractor titles before showing them in Discord."""
        cleaned = (title or fallback or "").strip()
        if not cleaned:
            cleaned = fallback

        if is_bilibili_url(source_url):
            cleaned = re.sub(r"\s*[_-]\s*哔哩哔哩(?:_bilibili)?\s*$", "", cleaned)
            cleaned = re.sub(r"\s*\|\s*哔哩哔哩(?:\s*bilibili)?\s*$", "", cleaned, flags=re.I)
            cleaned = re.sub(r"\s*-\s*bilibili\s*$", "", cleaned, flags=re.I)

        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or fallback

    def _is_position_like_title(self, title: str) -> bool:
        """Bilibili multipart entries can expose only a page/position label."""
        value = (title or "").strip().lower()
        if not value:
            return True
        return bool(
            re.fullmatch(r"(?:p|part|ep)?\s*\d{1,4}", value)
            or re.fullmatch(r"第\s*\d{1,4}\s*(?:p|集|话|段|部分)", value)
        )

    def _title_from_info(self, info: dict, fallback: str, source_url: str = "") -> str:
        if is_bilibili_url(source_url):
            title = (
                info.get("fulltitle")
                or info.get("title")
                or info.get("alt_title")
                or info.get("part")
                or fallback
            )
        else:
            title = (
                info.get("title")
                or info.get("fulltitle")
                or info.get("alt_title")
                or info.get("part")
                or fallback
            )
        return self._clean_song_title(title, source_url, fallback)

    def _create_song(self, input_str: str, title: str, requester) -> Song:
        """把解析结果包装成队列 Song 对象。"""
        return Song(
            input=input_str,
            title=self._clean_song_title(title, input_str),
            requester_id=requester.id,
            requester_name=requester.display_name,
            is_url=is_valid_url(input_str),
            webpage_url=input_str if is_valid_url(input_str) else None,
        )

    def _create_song_from_info(
        self,
        input_str: str,
        info: dict,
        requester,
        fallback_title: str = "未知标题",
    ) -> Song:
        """Create a Song with display metadata when yt-dlp exposes it."""
        webpage_url = (
            info.get("webpage_url")
            or info.get("original_url")
            or (input_str if is_valid_url(input_str) else None)
        )
        title = self._title_from_info(info, fallback_title, str(webpage_url or input_str))
        return Song(
            input=input_str,
            title=title,
            requester_id=requester.id,
            requester_name=requester.display_name,
            is_url=is_valid_url(input_str),
            webpage_url=str(webpage_url) if webpage_url else None,
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
            uploader=info.get("uploader") or info.get("channel") or info.get("artist"),
        )

    def _queued_song_message(self, song: Song, queue_size: int) -> str:
        if queue_size <= 1:
            return f"✅ 已加入队列：**{song.title}**"
        return f"✅ 已加入队列：**{song.title}**（队列第 {queue_size} 首）"

    def _format_duration(self, seconds: Optional[int]) -> str:
        if not seconds:
            return "--:--"
        seconds = int(seconds)
        minutes, second = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minute:02d}:{second:02d}"
        return f"{minute}:{second:02d}"

    def _build_now_playing_embed(self, session: ChannelSession) -> discord.Embed:
        song = session.current_song
        title = "Now Playing" if song else "Music Player"
        embed = discord.Embed(title=f"💿 {title}", color=discord.Color.blurple())

        if song:
            display_title = discord.utils.escape_markdown(song.title[:300])
            if song.webpage_url:
                embed.description = f"[{display_title}]({song.webpage_url})"
            else:
                embed.description = f"**{display_title}**"
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            if song.uploader:
                embed.add_field(name="Source", value=song.uploader[:1024], inline=True)
            embed.add_field(name="Requested By", value=song.requester_name[:1024] or "-", inline=True)
            embed.add_field(name="Duration", value=self._format_duration(song.duration), inline=True)
        else:
            embed.description = "当前没有正在播放的歌曲"

        vc = session.vc
        if vc and vc.is_paused():
            state = "Paused"
        elif vc and vc.is_playing():
            state = "Playing"
        else:
            state = "Idle"

        embed.set_footer(
            text=(
                f"{state} · Volume {session.volume}% · Queue {len(session.queue)} · "
                f"Loop {session.loop_mode}"
            )
        )
        return embed

    async def _refresh_now_playing_panel(self, session: ChannelSession):
        if not session.panel_channel_id or not session.panel_message_id:
            return
        channel = self.bot.get_channel(session.panel_channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(session.panel_message_id)
            await message.edit(
                embed=self._build_now_playing_embed(session),
                view=NowPlayingView(self, self._session_status_key(session)),
            )
        except Exception as e:
            logger.debug("刷新播放面板失败: %s", e)
            session.panel_channel_id = None
            session.panel_message_id = None

    async def _send_or_update_now_playing_panel(self, text_channel, channel_id: str, session: ChannelSession):
        if not text_channel:
            return
        embed = self._build_now_playing_embed(session)
        view = NowPlayingView(self, channel_id)

        if session.panel_channel_id and session.panel_message_id:
            panel_channel = self.bot.get_channel(session.panel_channel_id)
            if panel_channel:
                try:
                    message = await panel_channel.fetch_message(session.panel_message_id)
                    await message.edit(embed=embed, view=view)
                    return
                except Exception as e:
                    logger.debug("更新播放面板失败: %s", e)

        message = await self._safe_send(text_channel, embed=embed, view=view)
        if message:
            session.panel_channel_id = message.channel.id
            session.panel_message_id = message.id

    async def _disable_now_playing_panel(self, session: ChannelSession, reason: str = "Stopped"):
        if not session.panel_channel_id or not session.panel_message_id:
            return
        channel = self.bot.get_channel(session.panel_channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(session.panel_message_id)
            embed = self._build_now_playing_embed(session)
            embed.title = f"💿 {reason}"
            view = NowPlayingView(self, self._session_status_key(session))
            for item in view.children:
                item.disabled = True
            await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.debug("禁用播放面板失败: %s", e)

    def _clone_song(self, song: Song) -> Song:
        """复制歌曲；有效缓存可被循环和重播继续使用。"""
        return Song(
            input=song.input,
            title=song.title,
            requester_id=song.requester_id,
            requester_name=song.requester_name,
            is_url=song.is_url,
            webpage_url=song.webpage_url,
            thumbnail=song.thumbnail,
            duration=song.duration,
            uploader=song.uploader,
            local_path=song.local_path,
            downloaded=bool(song.local_path and song.local_path.is_file()),
        )

    def _get_title_for_input(self, input_str: str) -> str:
        """获取本地文件名或远程链接标题。"""
        if not is_valid_url(input_str):
            name = input_str
            if name.endswith(".mp3"):
                name = name[:-4]
            return name

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=None,
                allow_playlist=False,
                extract_flat=False,
            )
            ydl_opts = self._apply_site_opts(input_str, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
                return self._title_from_info(info, input_str, input_str)
        except Exception:
            pass

        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "noplaylist": True,
            }
            ydl_opts = self._apply_site_opts(input_str, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
                return self._title_from_info(info, input_str, input_str)
        except Exception:
            return input_str

    def _get_song_for_input(self, input_str: str, requester) -> Song:
        """Resolve one local file or remote URL into a Song."""
        if not is_valid_url(input_str):
            title = input_str[:-4] if input_str.endswith(".mp3") else input_str
            return self._create_song(input_str, title, requester)

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=None,
                allow_playlist=False,
                extract_flat=False,
            )
            ydl_opts = self._apply_site_opts(input_str, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
            return self._create_song_from_info(input_str, info, requester, input_str)
        except Exception:
            title = self._get_title_for_input(input_str)
            return self._create_song(input_str, title, requester)

    def _local_music_exists(self, input_str: str) -> bool:
        """检查输入是否对应 assets/music 下的本地 mp3。"""
        return self._resolve_local_music(input_str) is not None

    def _resolve_local_music(self, input_str: str) -> Optional[Path]:
        name = input_str.strip()
        if not name or "/" in name or "\\" in name or ":" in name:
            return None
        if not name.lower().endswith(".mp3"):
            name += ".mp3"
        root = Path(MUSIC_DIR).resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            return None
        return path

    def _local_music_choices(self, query: str) -> List[app_commands.Choice[str]]:
        """根据输入返回本地 mp3 自动补全候选。"""
        query_lower = query.lower()
        choices = []

        for file_path in sorted(Path(MUSIC_DIR).glob("*.mp3")):
            name = file_path.stem
            if len(name) > 100 or not self._resolve_local_music(name):
                continue
            if query_lower and query_lower not in name.lower():
                continue
            choices.append(app_commands.Choice(name=f"本地：{name}"[:100], value=name))
            if len(choices) >= 5:
                break

        return choices

    def _search_youtube_song(self, query: str, requester) -> Song:
        """用 yt-dlp 搜索 YouTube 第一条结果，并转换成 Song。"""
        search_query = f"ytsearch1:{query}"

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=None,
                allow_playlist=False,
                extract_flat=True,
            )
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
        except Exception:
            try:
                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "extract_flat": True,
                    "noplaylist": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_query, download=False)
            except Exception as e:
                raise RuntimeError(f"YouTube 搜索失败：{e}")

        entries = info.get("entries") or []
        if not entries:
            raise RuntimeError("没有找到匹配的 YouTube 搜索结果")

        entry = entries[0]
        entry_url = self._youtube_search_entry_url(entry)

        if not entry_url:
            raise RuntimeError("搜索到了结果，但没有拿到可播放链接")

        return self._create_song_from_info(entry_url, entry, requester, query)

    def _search_youtube_results(self, query: str) -> List[dict]:
        """用 yt-dlp 返回完整 YouTube 搜索结果。"""
        cache_key = query.strip().lower()
        cached = self.search_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]

        search_query = f"ytsearch10:{query}"
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "extract_flat": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)

        results = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            title = entry.get("title")
            entry_url = self._youtube_search_entry_url(entry)

            if not title or not entry_url:
                continue
            results.append(
                {
                    "title": title,
                    "url": entry_url,
                    "thumbnail": entry.get("thumbnail"),
                    "duration": entry.get("duration"),
                    "uploader": entry.get("uploader") or entry.get("channel"),
                }
            )
            if len(results) >= 10:
                break

        if len(self.search_cache) >= 100:
            self.search_cache.pop(next(iter(self.search_cache)), None)
        self.search_cache[cache_key] = (time.monotonic(), results)
        return results

    def _search_youtube_choices(self, query: str) -> List[app_commands.Choice[str]]:
        """用 yt-dlp 返回 YouTube 搜索自动补全候选。"""
        choices = []
        for result in self._search_youtube_results(query):
            choices.append(
                app_commands.Choice(
                    name=f"YouTube：{result['title']}"[:100],
                    value=result["url"][:100],
                )
            )
        return choices

    def _extract_collection_songs(self, url: str, requester) -> List[Song]:
        """提取播放列表/合集；如果不是多条目，则退化成单曲。"""
        if is_youtube_url(url) and "list=" in url:
            url = self._normalize_youtube_playlist_url(url)

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=None,
                allow_playlist=True,
                extract_flat=True,
            )
            ydl_opts = self._apply_site_opts(url, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return self._parse_collection_info_to_songs(info, url, requester)
        except Exception:
            pass

        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "extract_flat": "in_playlist",
            }
            ydl_opts = self._apply_site_opts(url, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return self._parse_collection_info_to_songs(info, url, requester)
        except Exception as e:
            raise RuntimeError(f"提取列表失败：{e}")

    def _parse_collection_info_to_songs(
        self,
        info: dict,
        fallback_url: str,
        requester,
    ) -> List[Song]:
        """把 yt-dlp 返回的列表信息转换成 Song 列表。"""
        songs = []
        entries = info.get("entries")

        if not entries:
            songs.append(self._create_song_from_info(fallback_url, info, requester, fallback_url))
            return songs

        parent_title = self._title_from_info(info, fallback_url, fallback_url)

        for entry in entries:
            if not entry:
                continue

            if is_bilibili_url(fallback_url):
                entry_url = (
                    entry.get("webpage_url")
                    or entry.get("original_url")
                    or entry.get("url")
                )
            else:
                entry_url = (
                    entry.get("url")
                    or entry.get("webpage_url")
                    or entry.get("original_url")
                )
            title = self._title_from_info(entry, parent_title, str(entry_url or fallback_url))
            if is_bilibili_url(str(entry_url or fallback_url)) and self._is_position_like_title(title):
                title = parent_title

            # 有些 flat entry 给的是 id，不是完整链接。
            if entry_url and not str(entry_url).startswith("http"):
                webpage_url = entry.get("webpage_url")
                if webpage_url and str(webpage_url).startswith("http"):
                    entry_url = webpage_url
                else:
                    ie_key = entry.get("ie_key", "")
                    if "youtube" in str(ie_key).lower():
                        entry_url = f"https://www.youtube.com/watch?v={entry_url}"
                    else:
                        continue

            if not entry_url:
                continue

            song = self._create_song_from_info(entry_url, entry, requester, title)
            song.title = self._clean_song_title(title, entry_url)
            songs.append(song)

        if not songs:
            songs.append(self._create_song_from_info(fallback_url, info, requester, fallback_url))

        return songs

    def _download_song(self, song: Song) -> Path:
        """下载线程拥有文件生命周期；取消时清理含分片在内的输出。"""
        unique_name = uuid.uuid4().hex
        self.active_downloads.add(unique_name)
        result = None
        def check_cancelled(_=None):
            if song.cancelled or self.closing:
                raise RuntimeError("下载已取消")
        try:
            check_cancelled()
            opts = self._build_ydl_opts(str(self.cache_dir / f"{unique_name}.%(ext)s"))
            opts["progress_hooks"] = [check_cancelled]
            opts["postprocessor_hooks"] = [check_cancelled]
            opts = self._apply_site_opts(song.input, opts)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(song.input, download=True)
                check_cancelled()
                result = self._locate_downloaded_file(ydl, info, unique_name)
            song.local_path = result
            song.downloaded = True
            return result
        finally:
            self.active_downloads.discard(unique_name)
            if result is None or song.cancelled or self.closing:
                for path in self.cache_dir.glob(f"{unique_name}.*"):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("无法删除下载残留: %s", path)

    async def _download_song_limited(self, song: Song) -> Path:
        """预下载与前台共享同一任务；等待者取消不释放仍在使用的并发槽。"""
        if song.local_path and song.local_path.is_file():
            return song.local_path
        if not song.download_task or song.download_task.done():
            song.download_task = self._spawn(self._download_once(song))
        return await asyncio.shield(song.download_task)

    async def _download_once(self, song: Song) -> Path:
        song.downloading = True
        try:
            async with self.download_semaphore:
                if song.cancelled or self.closing:
                    raise RuntimeError("下载已取消")
                path = await self.bot.loop.run_in_executor(None, self._download_song, song)
                if song.cancelled or self.closing:
                    self._spawn(self._delete_file_later(path))
                    raise RuntimeError("下载已取消")
                song.local_path = path
                song.downloaded = True
                return path
        finally:
            song.downloading = False

    def _locate_downloaded_file(self, ydl, info: dict, unique_name: str) -> Path:
        """根据 yt-dlp 输出结果定位最终下载文件。"""
        downloaded_path = Path(ydl.prepare_filename(info))
        final_path = downloaded_path.with_suffix(".mp3")

        if final_path.exists():
            return final_path

        if downloaded_path.exists():
            return downloaded_path

        candidates = list(self.cache_dir.glob(f"{unique_name}.*"))
        if candidates:
            return candidates[0]

        raise FileNotFoundError("下载完成但未找到本地音频文件")

    # ===== 预下载与播放 =====

    async def _predownload_next_two(self, channel_id: str):
        """提前下载队首歌曲，共享下载任务避免出队时重复下载。"""
        session = self.sessions.get(channel_id)
        if not session or session.stopped:
            return
        async with session.queue_lock:
            targets = session.queue[:PREDOWNLOAD_COUNT]
        for song in targets:
            if session.stopped:
                return
            if song.cancelled or not song.is_url:
                continue
            try:
                await self._download_song_limited(song)
            except Exception as error:
                if not song.cancelled:
                    logger.warning("预下载失败: %s | %s", song.title, error)

    def _start_predownload_task(self, channel_id: int):
        """如果当前没有预下载任务，就启动一个后台预下载任务。"""
        session = self.sessions.get(channel_id)
        if not session or session.stopped:
            return

        if session.predownload_task and not session.predownload_task.done():
            return

        session.predownload_task = self.bot.loop.create_task(
            self._predownload_next_two(channel_id)
        )

    async def _ensure_connected(self, ctx, channel, session):
        """串行连接同一服务器；不抢占另一个频道正在使用的连接。"""
        lock = self.connection_locks.setdefault(ctx.guild.id, asyncio.Lock())
        async with lock:
            if self.closing or session.stopped or self.sessions.get(self._channel_key(channel)) is not session:
                return None
            self._remember_session_location(session, ctx, channel)
            self._cleanup_dead_vc(session)
            me = getattr(ctx.guild, "me", None)
            if me:
                permissions = channel.permissions_for(me)
                if not permissions.connect or not permissions.speak:
                    await ctx.send("❌ 我缺少这个频道的连接或说话权限。")
                    return None
            guild_vc = ctx.guild.voice_client
            if guild_vc and guild_vc.is_connected():
                if guild_vc.channel and guild_vc.channel.id != channel.id:
                    old_session = self.sessions.get(self._channel_key(guild_vc.channel))
                    if guild_vc.is_playing() or guild_vc.is_paused() or (old_session and (old_session.queue or old_session.preparing_song)):
                        await ctx.send("我正在另一个语音频道播放，请先在原频道使用 /stop。")
                        return None
                    await self._retire_guild_sessions(ctx.guild.id, channel.id)
                    try:
                        await guild_vc.move_to(channel)
                    except Exception as error:
                        await ctx.send(f"❌ 移动语音频道失败：{error}")
                        return None
                session.vc = guild_vc
            elif not session.vc:
                try:
                    session.vc = await channel.connect(timeout=20, reconnect=True)
                except Exception as error:
                    await ctx.send(f"❌ 连接语音失败：{error}")
                    return None
            if session.stopped or self.sessions.get(self._channel_key(channel)) is not session:
                if session.vc:
                    await session.vc.disconnect(force=True)
                session.vc = None
                return None
            self._touch_session(session, ctx, channel)
            self._start_idle_task(self._channel_key(channel))
            self._write_music_status(session)
            return session.vc

    async def _play_local_file(self, vc, file_path: Path, channel_id: str, current_song: Song):
        session = self.sessions.get(channel_id)
        if not session or session.stopped or current_song.cancelled:
            return
        play_generation = session.generation
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(str(file_path), executable=self.ffmpeg_path, options="-vn"),
            volume=session.volume / 100,
        )
        def after_play(error):
            # discord.py 在音频线程调用这里；所有会话操作回到事件循环。
            if not self.bot.loop.is_closed():
                self.bot.loop.call_soon_threadsafe(
                    self._spawn,
                    self._after_playback(channel_id, session, current_song, play_generation, error),
                )
        session.current_song = current_song
        session.preparing_song = None
        session.idle_since = None
        try:
            vc.play(source, after=after_play)
        except Exception:
            source.cleanup()
            session.current_song = None
            raise
        self._write_music_status(session)

    async def _after_playback(self, channel_id, session, song, generation, error):
        active = self.sessions.get(channel_id) is session and not session.stopped and session.generation == generation
        if active and session.current_song is song:
            async with session.queue_lock:
                if not error and not song.cancelled and not song.suppress_repeat and session.loop_mode in {"one", "queue"}:
                    if len(session.queue) < MAX_QUEUE_SIZE:
                        repeat = self._clone_song(song)
                        if session.loop_mode == "one":
                            session.queue.insert(0, repeat)
                        else:
                            session.queue.append(repeat)
                session.current_song = None
            self._write_music_status(session)
            await self._refresh_now_playing_panel(session)
        if song.is_url and song.local_path:
            path = song.local_path
            song.local_path = None
            song.downloaded = False
            self._spawn(self._delete_file_later(path))
        if error:
            logger.warning("音频播放失败: %s", error)
            if active:
                await self._safe_send(self._get_text_channel(session), "❌ 音频播放失败，正在尝试下一首。")
        if active:
            await self.play_next(channel_id)

    def _set_volume(self, session, volume):
        session.volume = volume
        source = getattr(session.vc, "source", None)
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = volume / 100

    # ===== 内部命令实现 =====

    async def _join_impl(self, ctx):
        """加入用户当前所在语音频道。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("先加入语音啊喵~")
            return

        session = self._get_session(channel)
        self._remember_session_location(session, ctx, channel)
        self._touch_session(session, ctx, channel)
        session.last_text_channel_id = ctx.channel.id
        session.stopped = False

        vc = await self._ensure_connected(ctx, channel, session)
        if not vc:
            return

        await ctx.send(f"✅ 我来啦！已加入：**{channel.name}**")

    async def _queue_collection_input(self, ctx, input: str, channel, session):
        """Queue a playlist/collection through the unified play command."""
        try:
            songs = await self.bot.loop.run_in_executor(
                None,
                self._extract_collection_songs,
                input,
                ctx.author,
            )

            if not songs:
                await ctx.send("❌ 没有提取到任何歌曲喵~")
                return

            original_count = len(songs)
            songs = self._dedupe_songs(songs)

            async with session.queue_lock:
                existing_keys = {self._song_key(song) for song in session.queue}
                if session.current_song:
                    existing_keys.add(self._song_key(session.current_song))
                songs = [song for song in songs if self._song_key(song) not in existing_keys]

                deduped_count = len(songs)
                if len(songs) > MAX_PLAYLIST_SONGS:
                    songs = random.sample(songs, MAX_PLAYLIST_SONGS)
                elif len(songs) > 1:
                    random.shuffle(songs)

                remaining_slots = MAX_QUEUE_SIZE - len(session.queue)
                if remaining_slots <= 0:
                    await ctx.send(f"队列已经满了喵~ 当前上限是 {MAX_QUEUE_SIZE} 首")
                    return

                limited_by_queue = len(songs) > remaining_slots
                songs = songs[:remaining_slots]

                if not songs:
                    await ctx.send("这些歌曲已经都在队列里了喵~")
                    return

                session.queue.extend(songs)
                queue_size = len(session.queue)

            self._log_session(
                channel.id,
                "playlist queued" if len(songs) > 1 else "song queued",
                guild_id=ctx.guild.id if ctx.guild else 0,
                added=len(songs),
                queue_size=queue_size,
            )
            self._write_music_status(session)

            if len(songs) == 1:
                await ctx.send(self._queued_song_message(songs[0], queue_size))
            else:
                note = f"✅ 已加入 **{len(songs)}** 首歌曲到队列"
                extras = []
                if original_count != deduped_count:
                    extras.append(f"去重 {original_count - deduped_count} 首")
                if deduped_count > MAX_PLAYLIST_SONGS:
                    extras.append(f"随机选取 {MAX_PLAYLIST_SONGS} 首")
                if limited_by_queue:
                    extras.append(f"受队列上限 {MAX_QUEUE_SIZE} 首限制")
                if extras:
                    note += f"（{'; '.join(extras)}）"
                await ctx.send(note)

            vc = await self._ensure_connected(ctx, channel, session)
            if not vc:
                return

            self._start_predownload_task(self._channel_key(channel))

            if not vc.is_playing() and not vc.is_paused():
                await self.play_next(self._channel_key(channel))

        except Exception as e:
            await ctx.send(f"❌ 读取列表失败：{e}")

    async def _play_impl(self, ctx, input: str):
        """添加一首歌曲到队列，并在空闲时开始播放。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("先加入语音啊喵~")
            return

        if not await self._check_cooldown(
            ctx,
            "play",
            COMMAND_COOLDOWNS["play"],
            channel,
            per_user=True,
        ):
            return

        session = self._get_session(channel)
        self._remember_session_location(session, ctx, channel)
        self._touch_session(session, ctx, channel)
        session.last_text_channel_id = ctx.channel.id
        session.stopped = False

        if len(session.queue) >= MAX_QUEUE_SIZE:
            await ctx.send(f"队列已经满了喵~ 当前上限是 {MAX_QUEUE_SIZE} 首")
            return

        if is_youtube_url(input):
            if self._is_youtube_radio_url(input):
                input = self._normalize_youtube_watch_url(input)
            elif self._is_youtube_playlist_like_url(input):
                await self._queue_collection_input(ctx, input, channel, session)
                return

        if is_bilibili_url(input):
            await self._queue_collection_input(ctx, input, channel, session)
            return

        if not is_valid_url(input) and not self._local_music_exists(input):
            await ctx.send(f"🔎 本地没有找到 `{input}`，正在 YouTube 搜索喵~")
            try:
                song = await self.bot.loop.run_in_executor(
                    None,
                    self._search_youtube_song,
                    input,
                    ctx.author,
                )
            except Exception as e:
                await ctx.send(f"❌ 搜索失败：{e}")
                return
        else:
            song = await self.bot.loop.run_in_executor(
                None,
                self._get_song_for_input,
                input,
                ctx.author,
            )

        async with session.queue_lock:
            if len(session.queue) >= MAX_QUEUE_SIZE:
                await ctx.send(f"队列已经满了喵~ 当前上限是 {MAX_QUEUE_SIZE} 首")
                return
            session.queue.append(song)
            queue_size = len(session.queue)

        self._log_session(
            channel.id,
            "song queued",
            guild_id=ctx.guild.id if ctx.guild else 0,
            queue_size=queue_size,
            title=song.title,
        )
        self._write_music_status(session)
        await ctx.send(self._queued_song_message(song, queue_size))

        vc = await self._ensure_connected(ctx, channel, session)
        if not vc:
            return

        self._start_predownload_task(self._channel_key(channel))

        if not vc.is_playing() and not vc.is_paused():
            await self.play_next(self._channel_key(channel))

    async def _queue_impl(self, ctx):
        """展示当前语音频道的播放队列。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self.sessions.get(self._channel_key(channel))
        if not session or (not session.queue and not session.current_song):
            await ctx.send("📭 队列是空的喵~")
            return

        async with session.queue_lock:
            current_song = session.current_song
            queued_songs = list(session.queue)

        lines = []
        if current_song:
            lines.append(
                f"正在播放：**{current_song.title}** — {current_song.requester_name}"
            )

        for i, song in enumerate(queued_songs, start=1):
            lines.append(f"{i}. **{song.title}** — {song.requester_name}")

        preview = lines[:16]
        more = len(lines) - len(preview)

        msg = f"🎶 **当前队列：** 音量 {session.volume}%\n" + "\n".join(preview)
        if more > 0:
            msg += f"\n… 还有 {more} 首未显示"

        await ctx.send(msg)

    async def _pause_impl(self, ctx):
        """暂停当前播放。"""
        session = self._get_existing_session(ctx)
        if not session or not session.vc or not session.vc.is_connected():
            await ctx.send("我现在没有在这个语音频道播放喵~")
            return

        if session.vc.is_paused():
            await ctx.send("已经是暂停状态啦喵~")
            return

        if not session.vc.is_playing():
            await ctx.send("当前没有正在播放的歌曲喵~")
            return

        session.vc.pause()
        await self._refresh_now_playing_panel(session)
        await ctx.send("⏸️ 已暂停")

    async def _resume_impl(self, ctx):
        """恢复当前暂停的播放。"""
        session = self._get_existing_session(ctx)
        if not session or not session.vc or not session.vc.is_connected():
            await ctx.send("我现在没有在这个语音频道播放喵~")
            return

        if not session.vc.is_paused():
            await ctx.send("当前没有暂停中的歌曲喵~")
            return

        session.vc.resume()
        await self._refresh_now_playing_panel(session)
        await ctx.send("▶️ 已继续播放")

    async def _now_impl(self, ctx):
        """显示当前正在播放的歌曲。"""
        session = self._get_existing_session(ctx)
        if not session or not session.current_song:
            await ctx.send("现在没有正在播放的歌曲喵~")
            return

        vc = session.vc
        state = "暂停中" if vc and vc.is_paused() else "播放中"
        song = session.current_song
        await ctx.send(
            f"🎧 **{state}：** **{song.title}**（by {song.requester_name}）\n"
            f"音量：{session.volume}%｜队列剩余：{len(session.queue)} 首"
        )

    async def _remove_impl(self, ctx, index: int):
        """从队列中移除指定编号的歌曲。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        if not await self._check_cooldown(
            ctx,
            "remove",
            COMMAND_COOLDOWNS["remove"],
            channel,
        ):
            return

        session = self.sessions.get(self._channel_key(channel))
        if not session or not session.queue:
            await ctx.send("队列是空的喵~")
            return

        if index < 1 or index > len(session.queue):
            await ctx.send(f"编号不对喵~ 请输入 1 到 {len(session.queue)} 之间的数字")
            return

        async with session.queue_lock:
            if index < 1 or index > len(session.queue):
                await ctx.send(f"编号不对喵~ 请输入 1 到 {len(session.queue)} 之间的数字")
                return
            song = session.queue.pop(index - 1)
        self._cancel_song(song)

        await ctx.send(f"🗑️ 已移除：**{song.title}**")
        self._start_predownload_task(self._channel_key(channel))

    async def _volume_impl(self, ctx, volume: int):
        """设置当前语音频道会话音量。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self._get_session(channel)
        session.volume = max(0, min(100, volume))
        await self._refresh_now_playing_panel(session)

        msg = f"🔊 音量已设置为 {session.volume}%"
        if session.vc and (session.vc.is_playing() or session.vc.is_paused()):
            msg += "（当前这首可能要下一首才完全生效）"
        await ctx.send(msg)

    async def _shuffle_impl(self, ctx):
        """随机打乱当前语音频道的待播放队列。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        if not await self._check_cooldown(
            ctx,
            "shuffle",
            COMMAND_COOLDOWNS["shuffle"],
            channel,
        ):
            return

        session = self.sessions.get(self._channel_key(channel))

        if not session or not session.queue:
            await ctx.send("队列是空的喵~")
            return

        async with session.queue_lock:
            random.shuffle(session.queue)
        await ctx.send("🔀 队列已随机打乱喵~")

    async def _help_impl(self, ctx):
        """显示普通用户能看懂的音乐命令帮助。"""
        await ctx.send(
            "**喵酱音乐 Bot 使用说明**\n"
            "`/play 歌名或链接`：播放本地歌、YouTube/Bilibili 链接，或搜索歌名\n"
            "`/queue`：查看队列\n"
            "`/skip`：跳过当前歌曲\n"
            "`/pause` / `/resume`：暂停或继续\n"
            "`/remove 编号`：删除队列里的某一首\n"
            "`/move 原编号 新编号`：调整队列顺序\n"
            "`/clear`：清空等待队列\n"
            "`/loop off|one|queue`：关闭循环、单曲循环、队列循环\n"
            "`/volume 0-100`：设置音量\n"
            "`/stop`：停止播放并离开频道\n\n"
            "所有命令也可以用 `!` 前缀，例如 `!play 稻香`。"
        )

    async def _clear_impl(self, ctx):
        """清空当前语音频道的等待队列，不打断正在播放的歌曲。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self.sessions.get(self._channel_key(channel))
        if not session or not session.queue:
            await ctx.send("队列已经是空的喵~")
            return

        self._touch_session(session, ctx, channel)
        async with session.queue_lock:
            removed = len(session.queue)
            for song in session.queue:
                self._cancel_song(song)
            session.queue.clear()

        self._write_music_status(session)
        await self._refresh_now_playing_panel(session)
        await ctx.send(f"🧹 已清空等待队列：{removed} 首")

    async def _move_impl(self, ctx, from_index: int, to_index: int):
        """调整队列中歌曲的位置。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self.sessions.get(self._channel_key(channel))
        if not session or not session.queue:
            await ctx.send("队列是空的喵~")
            return

        self._touch_session(session, ctx, channel)
        async with session.queue_lock:
            total = len(session.queue)
            if from_index < 1 or from_index > total or to_index < 1 or to_index > total:
                await ctx.send(f"编号不对喵~ 请输入 1 到 {total} 之间的数字")
                return
            song = session.queue.pop(from_index - 1)
            session.queue.insert(to_index - 1, song)

        self._write_music_status(session)
        await self._refresh_now_playing_panel(session)
        await ctx.send(f"↕️ 已移动：**{song.title}** 从 {from_index} 到 {to_index}")

    async def _loop_impl(self, ctx, mode: str):
        """设置循环模式。"""
        mode = mode.lower().strip()
        if mode not in {"off", "one", "queue"}:
            await ctx.send("循环模式只能是 `off`、`one` 或 `queue` 喵~")
            return

        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self._get_session(channel)
        self._touch_session(session, ctx, channel)
        session.loop_mode = mode
        self._write_music_status(session)
        await self._refresh_now_playing_panel(session)

        label = {"off": "已关闭循环", "one": "已开启单曲循环", "queue": "已开启队列循环"}[mode]
        await ctx.send(f"🔁 {label}")

    async def _replay_impl(self, ctx):
        """从头重播当前歌曲。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self.sessions.get(self._channel_key(channel))
        if not session or not session.current_song:
            await ctx.send("现在没有正在播放的歌曲喵~")
            return

        self._touch_session(session, ctx, channel)
        vc = session.vc
        if not vc or not vc.is_connected():
            await ctx.send("我还没加入语音频道喵~")
            return

        replay_song = self._clone_song(session.current_song)
        async with session.queue_lock:
            session.queue.insert(0, replay_song)

        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await ctx.send("🔂 已重新播放当前歌曲")
        else:
            await self.play_next(self._channel_key(channel))
            await ctx.send("🔂 已尝试重新播放当前歌曲")

    async def _skip_impl(self, ctx):
        """跳过当前歌曲；如果没有播放，则尝试播放下一首。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        if not await self._check_cooldown(
            ctx,
            "skip",
            COMMAND_COOLDOWNS["skip"],
            channel,
        ):
            return

        session = self.sessions.get(self._channel_key(channel))
        if not session:
            await ctx.send("我还没加入语音频道喵~ 先 /join 喵~")
            return

        self._touch_session(session, ctx, channel)
        self._cleanup_dead_vc(session)
        vc = session.vc
        if not vc or not vc.is_connected():
            await ctx.send("我还没加入语音频道喵~ 先 /join 喵~")
            return

        session.last_text_channel_id = ctx.channel.id

        if session.play_lock.locked():
            await ctx.send("正在切歌/准备下一首喵~ 请稍等一下")
            return

        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await ctx.send("⏭️ 跳过当前歌曲")
        else:
            await self.play_next(self._channel_key(channel))
            await ctx.send("▶️ 当前没有播放，已尝试播放下一首")

    async def _stop_impl(self, ctx):
        """停止播放、清空队列、断开语音连接并清理缓存。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先加入语音频道喵~")
            return

        if not await self._check_cooldown(
            ctx,
            "stop",
            COMMAND_COOLDOWNS["stop"],
            channel,
        ):
            return

        session = self.sessions.get(self._channel_key(channel))
        if not session:
            await ctx.send("我没有连接语音频道喵~")
            return

        self._cleanup_dead_vc(session)
        vc = session.vc
        session.stopped = True
        session.generation += 1

        async with session.queue_lock:
            for song in session.queue:
                self._cancel_song(song)
            session.queue.clear()

        if session.current_song:
            self._cancel_song(session.current_song)

        if session.predownload_task and not session.predownload_task.done():
            session.predownload_task.cancel()

        if session.idle_task and not session.idle_task.done():
            session.idle_task.cancel()

        await self._disable_now_playing_panel(session)

        if vc and vc.is_connected():
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await vc.disconnect()
            await ctx.send("🛑 已停止播放并离开频道")

            session.vc = None
            session.current_song = None
            self._write_music_status(None)
            self.sessions.pop(self._channel_key(channel), None)
            remove_music_session(self._channel_key(channel))
        else:
            await ctx.send("我没有连接语音频道喵~")

    # ===== 前缀命令：!join / !play ... =====

    @commands.command(name="join")
    async def join_prefix(self, ctx):
        await self._join_impl(ctx)

    @commands.command(name="play")
    async def play_prefix(self, ctx, *, input: str):
        await self._play_impl(ctx, input)

    @commands.command(name="queue")
    async def queue_prefix(self, ctx):
        await self._queue_impl(ctx)

    @commands.command(name="pause")
    async def pause_prefix(self, ctx):
        await self._pause_impl(ctx)

    @commands.command(name="resume")
    async def resume_prefix(self, ctx):
        await self._resume_impl(ctx)

    @commands.command(name="now")
    async def now_prefix(self, ctx):
        await self._now_impl(ctx)

    @commands.command(name="remove")
    async def remove_prefix(self, ctx, index: int):
        await self._remove_impl(ctx, index)

    @commands.command(name="volume")
    async def volume_prefix(self, ctx, volume: int):
        await self._volume_impl(ctx, volume)

    @commands.command(name="shuffle")
    async def shuffle_prefix(self, ctx):
        await self._shuffle_impl(ctx)

    @commands.command(name="skip")
    async def skip_prefix(self, ctx):
        await self._skip_impl(ctx)

    @commands.command(name="stop")
    async def stop_prefix(self, ctx):
        await self._stop_impl(ctx)

    @commands.command(name="help_music")
    async def help_music_prefix(self, ctx):
        await self._help_impl(ctx)

    @commands.command(name="clear")
    async def clear_prefix(self, ctx):
        await self._clear_impl(ctx)

    @commands.command(name="move")
    async def move_prefix(self, ctx, from_index: int, to_index: int):
        await self._move_impl(ctx, from_index, to_index)

    @commands.command(name="loop")
    async def loop_prefix(self, ctx, mode: str):
        await self._loop_impl(ctx, mode)

    # ===== slash commands：/join /play ... =====

    @app_commands.command(name="join", description="让 bot 加入你所在的语音频道")
    async def join_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._join_impl)

    @app_commands.command(name="play", description="播放链接、列表/合集、本地 mp3，或搜索关键词")
    @app_commands.describe(input="YouTube/Bilibili 链接或列表、本地 mp3 名称，或要搜索的歌曲关键词")
    @app_commands.autocomplete(input=play_input_autocomplete)
    async def play_slash(self, interaction: discord.Interaction, input: str):
        await self._run_slash(interaction, self._play_impl, input)

    @app_commands.command(name="queue", description="查看当前语音频道的播放队列")
    async def queue_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._queue_impl)

    @app_commands.command(name="pause", description="暂停当前歌曲")
    async def pause_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._pause_impl)

    @app_commands.command(name="resume", description="继续播放当前歌曲")
    async def resume_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._resume_impl)

    @app_commands.command(name="now", description="查看当前正在播放的歌曲")
    async def now_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._now_impl)

    @app_commands.command(name="remove", description="从队列移除指定编号的歌曲")
    @app_commands.describe(index="队列中的编号，从 1 开始")
    async def remove_slash(self, interaction: discord.Interaction, index: int):
        await self._run_slash(interaction, self._remove_impl, index)

    @app_commands.command(name="volume", description="设置播放音量 0-100")
    @app_commands.describe(volume="音量百分比，范围 0 到 100")
    async def volume_slash(self, interaction: discord.Interaction, volume: int):
        await self._run_slash(interaction, self._volume_impl, volume)

    @app_commands.command(name="shuffle", description="打乱当前播放队列")
    async def shuffle_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._shuffle_impl)

    @app_commands.command(name="skip", description="跳过当前播放并播放下一首")
    async def skip_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._skip_impl)

    @app_commands.command(name="stop", description="停止播放并清空当前频道的队列")
    async def stop_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._stop_impl)

    @app_commands.command(name="help_music", description="查看音乐 Bot 的使用说明")
    async def help_music_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._help_impl)

    @app_commands.command(name="clear", description="清空当前语音频道的等待队列")
    async def clear_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._clear_impl)

    @app_commands.command(name="move", description="调整队列中歌曲的位置")
    @app_commands.describe(from_index="原来的队列编号", to_index="新的队列编号")
    async def move_slash(
        self,
        interaction: discord.Interaction,
        from_index: int,
        to_index: int,
    ):
        await self._run_slash(interaction, self._move_impl, from_index, to_index)

    @app_commands.command(name="loop", description="设置循环模式：关闭、单曲、队列")
    @app_commands.describe(mode="off=关闭，one=单曲循环，queue=队列循环")
    async def loop_slash(self, interaction: discord.Interaction, mode: str):
        await self._run_slash(interaction, self._loop_impl, mode)

    # ===== 播放调度 =====

    async def play_next(self, channel_id: int):
        """从队列中取下一首，并按本地文件/远程链接两种路径播放。"""
        session = self.sessions.get(channel_id)
        if not session:
            return

        if session.play_lock.locked():
            return

        async with session.play_lock:
            while True:
                if session.stopped:
                    return

                self._cleanup_dead_vc(session)
                vc = session.vc
                text_channel = self._get_text_channel(session)

                if not vc or not vc.is_connected():
                    if text_channel:
                        await text_channel.send("我现在不在语音里喵~ 先用 /join 再 /play 继续吧")
                    return

                if vc.is_playing() or vc.is_paused():
                    return

                async with session.queue_lock:
                    if not session.queue:
                        return
                    song = session.queue.pop(0)
                if song.cancelled:
                    continue

                # 本地 mp3：直接从 assets/music 目录读取。
                if not song.is_url:
                    name = song.input
                    if not name.endswith(".mp3"):
                        name += ".mp3"

                    file_path = Path(MUSIC_DIR) / name
                    if not file_path.exists():
                        if text_channel:
                            await text_channel.send(f"❌ 找不到文件：`{name}`")
                        continue

                    try:
                        await self._play_local_file(vc, file_path, channel_id, song)
                        if text_channel:
                            await self._send_or_update_now_playing_panel(text_channel, channel_id, session)
                        self._start_predownload_task(channel_id)
                        return
                    except Exception as e:
                        if text_channel:
                            await text_channel.send(f"❌ 本地文件播放失败：{e}")
                        continue

                # 远程链接：优先使用预下载好的缓存文件，否则现场下载。
                try:
                    if song.local_path and song.local_path.exists():
                        await self._play_local_file(vc, song.local_path, channel_id, song)
                        if text_channel:
                            await self._send_or_update_now_playing_panel(text_channel, channel_id, session)
                        self._start_predownload_task(channel_id)
                        return

                    if song.downloading:
                        waited = 0
                        while song.downloading and waited < 20:
                            if session.stopped or song.cancelled:
                                return
                            await asyncio.sleep(0.5)
                            waited += 0.5

                    if session.stopped or song.cancelled:
                        continue

                    if song.local_path and song.local_path.exists():
                        await self._play_local_file(vc, song.local_path, channel_id, song)
                        if text_channel:
                            await self._send_or_update_now_playing_panel(text_channel, channel_id, session)
                        self._start_predownload_task(channel_id)
                        return

                    song.downloading = True
                    local_path = await self._download_song_limited(song)
                    if session.stopped or song.cancelled:
                        self._spawn(self._delete_file_later(local_path))
                        song.downloading = False
                        continue
                    song.local_path = local_path
                    song.downloaded = True
                    song.downloading = False

                    await self._play_local_file(vc, local_path, channel_id, song)
                    if text_channel:
                        await self._send_or_update_now_playing_panel(text_channel, channel_id, session)

                    self._start_predownload_task(channel_id)
                    return

                except Exception as e:
                    song.downloading = False
                    if text_channel:
                        await text_channel.send(f"❌ 下载或播放失败：{e}")
                    if song.local_path:
                        self._spawn(self._delete_file_later(song.local_path))
                        song.local_path = None
                    continue


async def setup(bot):
    """discord.py 加载扩展时调用。"""
    await bot.add_cog(Music(bot))
    print("✅ Music cog 已成功加载")
