"""音乐播放功能。

支持本地 mp3、YouTube / Bilibili 单曲、播放列表/合集、队列管理和预下载。
这个模块以语音频道为单位维护播放会话，避免不同语音频道的队列互相影响。
"""

import asyncio
import os
import random
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from bot.path import MUSIC_DIR


# ===== 基础配置 =====

server_id = 1298956383819010090
guild_object = discord.Object(id=server_id)
DEFAULT_VOLUME = 10


# ===== URL 判断工具 =====

def is_valid_url(url: str) -> bool:
    """判断输入是否是 http/https 链接。"""
    return isinstance(url, str) and (
        url.startswith("http://") or url.startswith("https://")
    )


def is_youtube_url(url: str) -> bool:
    """判断输入是否是 YouTube 链接。"""
    if not isinstance(url, str):
        return False
    return "youtube.com" in url or "youtu.be" in url


def is_bilibili_url(url: str) -> bool:
    """判断输入是否是 Bilibili 链接。"""
    if not isinstance(url, str):
        return False
    return "bilibili.com" in url or "b23.tv" in url


# ===== 数据结构 =====

@dataclass
class Song:
    """队列中的单首歌曲。"""

    input: str
    title: str
    requester_id: int
    requester_name: str
    is_url: bool
    local_path: Optional[Path] = None
    downloaded: bool = False
    downloading: bool = False


@dataclass
class ChannelSession:
    """一个语音频道对应一个播放会话。"""

    queue: List[Song] = field(default_factory=list)
    vc: Optional[discord.VoiceClient] = None
    last_text_channel_id: Optional[int] = None
    current_song: Optional[Song] = None
    predownload_task: Optional[asyncio.Task] = None
    volume: int = DEFAULT_VOLUME


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


class Music(commands.Cog):
    """音乐命令与播放调度。"""

    def __init__(self, bot):
        self.bot = bot
        self.sessions: Dict[int, ChannelSession] = {}
        self.search_cache = {}

        self.cache_dir = Path(MUSIC_DIR) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        if os.name == "nt":
            self.ffmpeg_path = "C:/Program Files/ffmpeg/bin/ffmpeg.exe"
        else:
            self.ffmpeg_path = "ffmpeg"

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
        if not ctx.author.voice:
            return None
        return ctx.author.voice.channel

    def _get_session(self, channel_id: int) -> ChannelSession:
        """获取指定语音频道的会话，不存在则创建。"""
        if channel_id not in self.sessions:
            self.sessions[channel_id] = ChannelSession()
        return self.sessions[channel_id]

    def _get_existing_session(self, ctx) -> Optional[ChannelSession]:
        """根据用户当前语音频道取已有会话。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            return None
        return self.sessions.get(channel.id)

    def _cleanup_dead_vc(self, session: ChannelSession):
        """清理已经断开的 VoiceClient 引用。"""
        if session.vc and not session.vc.is_connected():
            session.vc = None

    def _get_text_channel(self, session: ChannelSession):
        """取回最近一次发起音乐命令的文字频道。"""
        if session.last_text_channel_id:
            return self.bot.get_channel(session.last_text_channel_id)
        return None

    def _delete_file_safely(self, file_path: Optional[Path]):
        """删除缓存文件，失败时只打印日志，不影响播放流程。"""
        if not file_path:
            return
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception as e:
            print(f"删除缓存文件失败: {file_path} | {e}")

    # ===== yt-dlp 解析与下载 =====

    def _normalize_youtube_playlist_url(self, url: str) -> str:
        """把 watch?v=xxx&list=yyy 形式统一成标准 playlist URL。"""
        match = re.search(r"list=([A-Za-z0-9_\-]+)", url)
        if match:
            list_id = match.group(1)
            return f"https://www.youtube.com/playlist?list={list_id}"
        return url

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
        }

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
                        "preferredquality": "192",
                    }
                ],
            })

        return opts

    def _create_song(self, input_str: str, title: str, requester) -> Song:
        """把解析结果包装成队列 Song 对象。"""
        return Song(
            input=input_str,
            title=title,
            requester_id=requester.id,
            requester_name=requester.display_name,
            is_url=is_valid_url(input_str),
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
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
                return info.get("title") or input_str
        except Exception:
            pass

        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
                return info.get("title") or input_str
        except Exception:
            return input_str

    def _local_music_exists(self, input_str: str) -> bool:
        """检查输入是否对应 assets/music 下的本地 mp3。"""
        name = input_str
        if not name.endswith(".mp3"):
            name += ".mp3"
        return (Path(MUSIC_DIR) / name).exists()

    def _local_music_choices(self, query: str) -> List[app_commands.Choice[str]]:
        """根据输入返回本地 mp3 自动补全候选。"""
        query_lower = query.lower()
        choices = []

        for file_path in sorted(Path(MUSIC_DIR).glob("*.mp3")):
            name = file_path.stem
            if query_lower and query_lower not in name.lower():
                continue
            choices.append(app_commands.Choice(name=f"本地：{name}", value=name))
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
        entry_url = (
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
        )
        if entry_url and not str(entry_url).startswith("http"):
            entry_url = f"https://www.youtube.com/watch?v={entry_url}"

        if not entry_url:
            raise RuntimeError("搜索到了结果，但没有拿到可播放链接")

        title = entry.get("title") or query
        return self._create_song(entry_url, title, requester)

    def _search_youtube_choices(self, query: str) -> List[app_commands.Choice[str]]:
        """用 yt-dlp 返回 YouTube 搜索自动补全候选。"""
        cache_key = query.strip().lower()
        if cache_key in self.search_cache:
            return self.search_cache[cache_key]

        search_query = f"ytsearch5:{query}"
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)

        choices = []
        for entry in info.get("entries") or []:
            title = entry.get("title")
            entry_url = (
                entry.get("webpage_url")
                or entry.get("original_url")
                or entry.get("url")
            )

            if not title or not entry_url:
                continue
            if not str(entry_url).startswith("http"):
                entry_url = f"https://www.youtube.com/watch?v={entry_url}"

            choices.append(
                app_commands.Choice(
                    name=f"YouTube：{title}"[:100],
                    value=str(entry_url)[:100],
                )
            )
            if len(choices) >= 5:
                break

        self.search_cache[cache_key] = choices
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
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return self._parse_collection_info_to_songs(info, url, requester)
        except Exception:
            pass

        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": "in_playlist",
            }
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
            title = info.get("title") or fallback_url
            songs.append(self._create_song(fallback_url, title, requester))
            return songs

        for entry in entries:
            if not entry:
                continue

            entry_url = (
                entry.get("url")
                or entry.get("webpage_url")
                or entry.get("original_url")
            )
            title = entry.get("title") or "未知标题"

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

            songs.append(self._create_song(entry_url, title, requester))

        if not songs:
            title = info.get("title") or fallback_url
            songs.append(self._create_song(fallback_url, title, requester))

        return songs

    def _download_song(self, song: Song) -> Path:
        """同步下载远程音频到本地缓存，并返回最终文件路径。"""
        unique_name = uuid.uuid4().hex
        output_template = str(self.cache_dir / f"{unique_name}.%(ext)s")

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=output_template,
                allow_playlist=False,
                extract_flat=False,
            )
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(song.input, download=True)
                return self._locate_downloaded_file(ydl, info, unique_name)
        except Exception:
            pass

        try:
            ydl_opts = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "outtmpl": output_template,
                "restrictfilenames": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(song.input, download=True)
                return self._locate_downloaded_file(ydl, info, unique_name)
        except Exception as e:
            raise RuntimeError(f"下载失败：{e}")

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

    async def _predownload_next_two(self, channel_id: int):
        """后台预下载队列前两首远程歌曲，减少下一首等待时间。"""
        session = self.sessions.get(channel_id)
        if not session:
            return

        targets = session.queue[:2]

        for song in targets:
            if not song.is_url:
                continue
            if song.downloaded or song.downloading or song.local_path is not None:
                continue

            song.downloading = True
            try:
                local_path = await self.bot.loop.run_in_executor(
                    None,
                    self._download_song,
                    song,
                )
                song.local_path = local_path
                song.downloaded = True
            except Exception as e:
                print(f"预下载失败: {song.title} | {e}")
            finally:
                song.downloading = False

    def _start_predownload_task(self, channel_id: int):
        """如果当前没有预下载任务，就启动一个后台预下载任务。"""
        session = self.sessions.get(channel_id)
        if not session:
            return

        if session.predownload_task and not session.predownload_task.done():
            return

        session.predownload_task = self.bot.loop.create_task(
            self._predownload_next_two(channel_id)
        )

    async def _ensure_connected(
        self,
        ctx,
        channel: discord.VoiceChannel,
        session: ChannelSession,
    ) -> Optional[discord.VoiceClient]:
        """确保 Bot 已连接到用户当前语音频道。"""
        self._cleanup_dead_vc(session)

        guild_vc = ctx.guild.voice_client
        if guild_vc and guild_vc.is_connected():
            if guild_vc.channel and guild_vc.channel.id == channel.id:
                session.vc = guild_vc
                return session.vc

            try:
                await guild_vc.move_to(channel)
                session.vc = guild_vc
                return session.vc
            except Exception:
                try:
                    await guild_vc.disconnect(force=True)
                except Exception:
                    pass

        if not session.vc:
            try:
                session.vc = await channel.connect()
            except Exception as e:
                await ctx.send(f"❌ 连接语音失败：{e}")
                return None

        return session.vc

    async def _play_local_file(
        self,
        vc: discord.VoiceClient,
        file_path: Path,
        channel_id: int,
        current_song: Song,
    ):
        """播放本地文件，并在播放结束后自动调度下一首。"""
        session = self.sessions.get(channel_id)
        volume = (session.volume if session else DEFAULT_VOLUME) / 100
        ffmpeg_options = {
            "options": f'-vn -af "volume={volume}"'
        }

        if session:
            session.current_song = current_song

        def after_play(error):
            if error:
                print(f"播放结束回调错误: {error}")

            # URL 缓存播放完后删除，避免缓存目录无限增长。
            if current_song.is_url and current_song.local_path:
                self._delete_file_safely(current_song.local_path)
                current_song.local_path = None
                current_song.downloaded = False

            if session:
                session.current_song = None

            self.bot.loop.create_task(self.play_next(channel_id))

        vc.play(
            discord.FFmpegPCMAudio(
                str(file_path),
                executable=self.ffmpeg_path,
                **ffmpeg_options,
            ),
            after=after_play,
        )

    # ===== 内部命令实现 =====

    async def _join_impl(self, ctx):
        """加入用户当前所在语音频道。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("先加入语音啊喵~")
            return

        session = self._get_session(channel.id)
        session.last_text_channel_id = ctx.channel.id

        vc = await self._ensure_connected(ctx, channel, session)
        if not vc:
            return

        await ctx.send(f"✅ 我来啦！已加入：**{channel.name}**")

    async def _play_impl(self, ctx, input: str):
        """添加一首歌曲到队列，并在空闲时开始播放。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("先加入语音啊喵~")
            return

        session = self._get_session(channel.id)
        session.last_text_channel_id = ctx.channel.id

        if is_bilibili_url(input):
            await ctx.send("📺 检测到 Bilibili 链接，正在尝试解析音频喵~")
        elif is_youtube_url(input):
            if "list=" in input:
                await ctx.send("这看起来像播放列表链接喵~ 请用 `/play_list 链接`")
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
            title = self._get_title_for_input(input)
            song = Song(
                input=input,
                title=title,
                requester_id=ctx.author.id,
                requester_name=ctx.author.display_name,
                is_url=is_valid_url(input),
            )

        session.queue.append(song)
        await ctx.send(f"✅ 添加到播放队列：**{song.title}**（by {song.requester_name}）")

        vc = await self._ensure_connected(ctx, channel, session)
        if not vc:
            return

        self._start_predownload_task(channel.id)

        if not vc.is_playing() and not vc.is_paused():
            await self.play_next(channel.id)

    async def _play_list_impl(self, ctx, collection_url: str):
        """读取播放列表/合集，打乱后批量加入队列。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("先加入语音啊喵~")
            return

        if not is_valid_url(collection_url):
            await ctx.send("❌ 这看起来不是有效链接喵~")
            return

        session = self._get_session(channel.id)
        session.last_text_channel_id = ctx.channel.id

        vc = await self._ensure_connected(ctx, channel, session)
        if not vc:
            return

        await ctx.send("📂 正在读取列表/合集，请稍候...")

        try:
            songs = await self.bot.loop.run_in_executor(
                None,
                self._extract_collection_songs,
                collection_url,
                ctx.author,
            )

            if not songs:
                await ctx.send("❌ 没有提取到任何歌曲喵~")
                return

            random.shuffle(songs)
            session.queue.extend(songs)

            await ctx.send(f"✅ 已加入 **{len(songs)}** 首歌曲，并已随机打乱顺序喵~")

            self._start_predownload_task(channel.id)

            if not vc.is_playing() and not vc.is_paused():
                await self.play_next(channel.id)

        except Exception as e:
            await ctx.send(f"❌ 读取列表失败：{e}")

    async def _queue_impl(self, ctx):
        """展示当前语音频道的播放队列。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self.sessions.get(channel.id)
        if not session or (not session.queue and not session.current_song):
            await ctx.send("📭 队列是空的喵~")
            return

        lines = []
        if session.current_song:
            lines.append(
                f"正在播放：**{session.current_song.title}** — {session.current_song.requester_name}"
            )

        for i, song in enumerate(session.queue, start=1):
            status = ""
            if song.downloading:
                status = " ⏳"
            elif song.downloaded or (song.local_path and song.local_path.exists()):
                status = " 📦"
            lines.append(f"{i}. **{song.title}** — {song.requester_name}{status}")

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

        session = self.sessions.get(channel.id)
        if not session or not session.queue:
            await ctx.send("队列是空的喵~")
            return

        if index < 1 or index > len(session.queue):
            await ctx.send(f"编号不对喵~ 请输入 1 到 {len(session.queue)} 之间的数字")
            return

        song = session.queue.pop(index - 1)
        if song.is_url and song.local_path:
            self._delete_file_safely(song.local_path)
            song.local_path = None

        await ctx.send(f"🗑️ 已移除：**{song.title}**")
        self._start_predownload_task(channel.id)

    async def _volume_impl(self, ctx, volume: int):
        """设置当前语音频道会话音量。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self._get_session(channel.id)
        session.volume = max(0, min(100, volume))

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

        session = self.sessions.get(channel.id)

        if not session or not session.queue:
            await ctx.send("队列是空的喵~")
            return

        random.shuffle(session.queue)
        await ctx.send("🔀 队列已随机打乱喵~")

    async def _skip_impl(self, ctx):
        """跳过当前歌曲；如果没有播放，则尝试播放下一首。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self.sessions.get(channel.id)
        if not session:
            await ctx.send("我还没加入语音频道喵~ 先 /join 喵~")
            return

        self._cleanup_dead_vc(session)
        vc = session.vc
        if not vc or not vc.is_connected():
            await ctx.send("我还没加入语音频道喵~ 先 /join 喵~")
            return

        session.last_text_channel_id = ctx.channel.id

        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await ctx.send("⏭️ 跳过当前歌曲")
        else:
            await self.play_next(channel.id)
            await ctx.send("▶️ 当前没有播放，已尝试播放下一首")

    async def _stop_impl(self, ctx):
        """停止播放、清空队列、断开语音连接并清理缓存。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先加入语音频道喵~")
            return

        session = self.sessions.get(channel.id)
        if not session:
            await ctx.send("我没有连接语音频道喵~")
            return

        self._cleanup_dead_vc(session)
        vc = session.vc

        for song in session.queue:
            if song.is_url and song.local_path:
                self._delete_file_safely(song.local_path)
                song.local_path = None

        if session.current_song and session.current_song.is_url and session.current_song.local_path:
            self._delete_file_safely(session.current_song.local_path)
            session.current_song.local_path = None

        if session.predownload_task and not session.predownload_task.done():
            session.predownload_task.cancel()

        if vc and vc.is_connected():
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await vc.disconnect()
            await ctx.send("🛑 已停止播放并离开频道")

            session.queue.clear()
            session.vc = None
            session.current_song = None
            self.sessions.pop(channel.id, None)
        else:
            await ctx.send("我没有连接语音频道喵~")

    # ===== 前缀命令：!join / !play ... =====

    @commands.command(name="join")
    async def join_prefix(self, ctx):
        await self._join_impl(ctx)

    @commands.command(name="play")
    async def play_prefix(self, ctx, *, input: str):
        await self._play_impl(ctx, input)

    @commands.command(name="play_list")
    async def play_list_prefix(self, ctx, *, collection_url: str):
        await self._play_list_impl(ctx, collection_url)

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

    # ===== slash commands：/join /play ... =====

    @app_commands.command(name="join", description="让 bot 加入你所在的语音频道")
    @app_commands.guilds(guild_object)
    async def join_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._join_impl)

    @app_commands.command(name="play", description="播放链接、本地 mp3，或搜索关键词")
    @app_commands.describe(input="YouTube/Bilibili 链接、本地 mp3 名称，或要搜索的歌曲关键词")
    @app_commands.guilds(guild_object)
    @app_commands.autocomplete(input=play_input_autocomplete)
    async def play_slash(self, interaction: discord.Interaction, input: str):
        await self._run_slash(interaction, self._play_impl, input)

    @app_commands.command(name="play_list", description="添加播放列表/合集并随机打乱")
    @app_commands.describe(collection_url="YouTube 播放列表、Bilibili 合集或其他可解析合集链接")
    @app_commands.guilds(guild_object)
    async def play_list_slash(self, interaction: discord.Interaction, collection_url: str):
        await self._run_slash(interaction, self._play_list_impl, collection_url)

    @app_commands.command(name="queue", description="查看当前语音频道的播放队列")
    @app_commands.guilds(guild_object)
    async def queue_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._queue_impl)

    @app_commands.command(name="pause", description="暂停当前歌曲")
    @app_commands.guilds(guild_object)
    async def pause_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._pause_impl)

    @app_commands.command(name="resume", description="继续播放当前歌曲")
    @app_commands.guilds(guild_object)
    async def resume_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._resume_impl)

    @app_commands.command(name="now", description="查看当前正在播放的歌曲")
    @app_commands.guilds(guild_object)
    async def now_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._now_impl)

    @app_commands.command(name="remove", description="从队列移除指定编号的歌曲")
    @app_commands.describe(index="队列中的编号，从 1 开始")
    @app_commands.guilds(guild_object)
    async def remove_slash(self, interaction: discord.Interaction, index: int):
        await self._run_slash(interaction, self._remove_impl, index)

    @app_commands.command(name="volume", description="设置播放音量 0-100")
    @app_commands.describe(volume="音量百分比，范围 0 到 100")
    @app_commands.guilds(guild_object)
    async def volume_slash(self, interaction: discord.Interaction, volume: int):
        await self._run_slash(interaction, self._volume_impl, volume)

    @app_commands.command(name="shuffle", description="打乱当前播放队列")
    @app_commands.guilds(guild_object)
    async def shuffle_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._shuffle_impl)

    @app_commands.command(name="skip", description="跳过当前播放并播放下一首")
    @app_commands.guilds(guild_object)
    async def skip_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._skip_impl)

    @app_commands.command(name="stop", description="停止播放并清空当前频道的队列")
    @app_commands.guilds(guild_object)
    async def stop_slash(self, interaction: discord.Interaction):
        await self._run_slash(interaction, self._stop_impl)

    # ===== 播放调度 =====

    async def play_next(self, channel_id: int):
        """从队列中取下一首，并按本地文件/远程链接两种路径播放。"""
        session = self.sessions.get(channel_id)
        if not session or not session.queue:
            return

        song = session.queue.pop(0)

        self._cleanup_dead_vc(session)
        vc = session.vc
        text_channel = self._get_text_channel(session)

        if not vc or not vc.is_connected():
            if text_channel:
                await text_channel.send("我现在不在语音里喵~ 先用 /join 再 /play 继续吧")
            return

        # 本地 mp3：直接从 assets/music 目录读取。
        if not song.is_url:
            name = song.input
            if not name.endswith(".mp3"):
                name += ".mp3"

            file_path = Path(MUSIC_DIR) / name
            if not file_path.exists():
                if text_channel:
                    await text_channel.send(f"❌ 找不到文件：`{name}`")
                await self.play_next(channel_id)
                return

            try:
                await self._play_local_file(vc, file_path, channel_id, song)
                if text_channel:
                    await text_channel.send(
                        f"🎵 正在播放本地文件：**{song.title}**（by {song.requester_name}）"
                    )
                self._start_predownload_task(channel_id)
            except Exception as e:
                if text_channel:
                    await text_channel.send(f"❌ 本地文件播放失败：{e}")
                await self.play_next(channel_id)
            return

        # 远程链接：优先使用预下载好的缓存文件，否则现场下载。
        try:
            if song.local_path and song.local_path.exists():
                await self._play_local_file(vc, song.local_path, channel_id, song)
                if text_channel:
                    await text_channel.send(f"🎶 正在播放：**{song.title}**（by {song.requester_name}）")
                self._start_predownload_task(channel_id)
                return

            if text_channel:
                await text_channel.send(f"📥 正在下载：**{song.title}**")

            song.downloading = True
            local_path = await self.bot.loop.run_in_executor(None, self._download_song, song)
            song.local_path = local_path
            song.downloaded = True
            song.downloading = False

            await self._play_local_file(vc, local_path, channel_id, song)
            if text_channel:
                await text_channel.send(f"🎶 正在播放：**{song.title}**（by {song.requester_name}）")

            self._start_predownload_task(channel_id)

        except Exception as e:
            song.downloading = False
            if text_channel:
                await text_channel.send(f"❌ 下载或播放失败：{e}")
            if song.local_path:
                self._delete_file_safely(song.local_path)
                song.local_path = None
            await self.play_next(channel_id)


async def setup(bot):
    """discord.py 加载扩展时调用。"""
    await bot.add_cog(Music(bot))
    print("✅ Music cog 已成功加载")
