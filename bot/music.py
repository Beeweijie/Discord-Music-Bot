import discord
from discord.ext import commands
import yt_dlp
import re
import random
import asyncio
import os
import uuid

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from pathlib import Path

from bot.path import MUSIC_DIR

server_id = 1298956383819010090


def is_valid_url(url: str) -> bool:
    regex = re.compile(
        r'^(https?:\/\/)'
        r'(([a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,})'
        r'(\/[^\s]*)?$'
    )
    return re.match(regex, url) is not None


@dataclass
class Song:
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
    queue: List[Song] = field(default_factory=list)
    vc: Optional[discord.VoiceClient] = None
    last_text_channel_id: Optional[int] = None
    current_song: Optional[Song] = None
    predownload_task: Optional[asyncio.Task] = None


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sessions: Dict[int, ChannelSession] = {}

        self.cache_dir = Path(MUSIC_DIR) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Windows / Linux 自动适配 ffmpeg
        if os.name == "nt":
            self.ffmpeg_path = "C:/Program Files/ffmpeg/bin/ffmpeg.exe"
        else:
            self.ffmpeg_path = "ffmpeg"

    # ========= 私有工具函数 =========

    def _get_user_voice_channel(self, ctx) -> Optional[discord.VoiceChannel]:
        if not ctx.author.voice:
            return None
        return ctx.author.voice.channel

    def _get_session(self, channel_id: int) -> ChannelSession:
        if channel_id not in self.sessions:
            self.sessions[channel_id] = ChannelSession()
        return self.sessions[channel_id]

    def _cleanup_dead_vc(self, session: ChannelSession):
        if session.vc and not session.vc.is_connected():
            session.vc = None

    def _get_text_channel(self, session: ChannelSession):
        if session.last_text_channel_id:
            return self.bot.get_channel(session.last_text_channel_id)
        return None

    def _delete_file_safely(self, file_path: Optional[Path]):
        if not file_path:
            return
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception as e:
            print(f"删除缓存文件失败: {file_path} | {e}")

    def _normalize_playlist_url(self, url: str) -> str:
        """
        如果用户给的是 watch?v=xxx&list=yyy 这种形式，
        转成更标准的 playlist URL。
        """
        match = re.search(r'list=([A-Za-z0-9_\-]+)', url)
        if match:
            list_id = match.group(1)
            return f"https://www.youtube.com/playlist?list={list_id}"
        return url

    def _get_title_for_input(self, input_str: str) -> str:
        # 本地文件：直接用文件名
        if not is_valid_url(input_str):
            name = input_str
            if name.endswith(".mp3"):
                name = name[:-4]
            return name

        # URL：轻量获取标题
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
                return info.get("title") or input_str
        except Exception:
            return input_str

    def _extract_playlist_songs(self, playlist_url: str, requester) -> List[Song]:
        """
        提取整个 playlist 的歌曲，但不下载。
        """
        playlist_url = self._normalize_playlist_url(playlist_url)

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
        }

        songs = []
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
            entries = info.get("entries", [])

            for entry in entries:
                if not entry:
                    continue

                video_url = entry.get("url")
                title = entry.get("title") or "未知标题"

                if video_url and not str(video_url).startswith("http"):
                    video_url = f"https://www.youtube.com/watch?v={video_url}"

                if not video_url:
                    continue

                songs.append(
                    Song(
                        input=video_url,
                        title=title,
                        requester_id=requester.id,
                        requester_name=requester.display_name,
                        is_url=True,
                    )
                )

        return songs

    def _download_song(self, song: Song) -> Path:
        """
        同步下载到本地，返回最终文件路径。
        """
        unique_name = uuid.uuid4().hex
        output_template = str(self.cache_dir / f"{unique_name}.%(ext)s")

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

    async def _predownload_next_two(self, channel_id: int):
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
                local_path = await self.bot.loop.run_in_executor(None, self._download_song, song)
                song.local_path = local_path
                song.downloaded = True
            except Exception as e:
                print(f"预下载失败: {song.title} | {e}")
            finally:
                song.downloading = False

    def _start_predownload_task(self, channel_id: int):
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
        session: ChannelSession
    ) -> Optional[discord.VoiceClient]:
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
        current_song: Song
    ):
        ffmpeg_options = {
            "options": '-vn -af "volume=0.1"'
        }

        session = self.sessions.get(channel_id)
        if session:
            session.current_song = current_song

        def after_play(error):
            if error:
                print(f"播放结束回调错误: {error}")

            # URL 缓存播放完删掉
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
                **ffmpeg_options
            ),
            after=after_play
        )

    # ========= 命令区 =========

    @commands.hybrid_command(
        name="join",
        description="让 bot 加入你所在的语音频道",
        guild=discord.Object(id=server_id)
    )
    async def join(self, ctx):
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

    @commands.hybrid_command(
        name="play",
        description="播放单曲或本地 mp3",
        guild=discord.Object(id=server_id)
    )
    async def play(self, ctx, input: str):
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("先加入语音啊喵~")
            return

        # 如果是播放列表链接，提示用 play_playlist
        if "list=" in input and "youtube.com" in input:
            await ctx.send("这看起来像播放列表链接喵~ 请用 `/play_playlist 链接`")
            return

        session = self._get_session(channel.id)
        session.last_text_channel_id = ctx.channel.id

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

        if not vc.is_playing():
            await self.play_next(channel.id)

    @commands.hybrid_command(
        name="play_playlist",
        description="添加整个播放列表并随机打乱",
        guild=discord.Object(id=server_id)
    )
    async def play_playlist(self, ctx, playlist_url: str):
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("先加入语音啊喵~")
            return

        if not is_valid_url(playlist_url):
            await ctx.send("❌ 这看起来不是有效链接喵~")
            return

        session = self._get_session(channel.id)
        session.last_text_channel_id = ctx.channel.id

        vc = await self._ensure_connected(ctx, channel, session)
        if not vc:
            return

        await ctx.send("📂 正在读取播放列表，请稍候...")

        try:
            songs = await self.bot.loop.run_in_executor(
                None,
                self._extract_playlist_songs,
                playlist_url,
                ctx.author
            )

            if not songs:
                await ctx.send("❌ 没有提取到任何歌曲喵~")
                return

            random.shuffle(songs)
            session.queue.extend(songs)

            await ctx.send(f"✅ 已加入 **{len(songs)}** 首歌曲，并已随机打乱顺序喵~")

            self._start_predownload_task(channel.id)

            if not vc.is_playing():
                await self.play_next(channel.id)

        except Exception as e:
            await ctx.send(f"❌ 读取播放列表失败：{e}")

    @commands.hybrid_command(
        name="queue",
        description="查看当前语音频道的播放队列",
        guild=discord.Object(id=server_id)
    )
    async def queue(self, ctx):
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self.sessions.get(channel.id)
        if not session or not session.queue:
            await ctx.send("📭 队列是空的喵~")
            return

        lines = []
        for i, song in enumerate(session.queue, start=1):
            status = ""
            if song.downloading:
                status = " ⏳"
            elif song.downloaded or (song.local_path and song.local_path.exists()):
                status = " 📦"
            lines.append(f"{i}. **{song.title}** — {song.requester_name}{status}")

        preview = lines[:15]
        more = len(lines) - len(preview)

        msg = "🎶 **当前队列：**\n" + "\n".join(preview)
        if more > 0:
            msg += f"\n… 还有 {more} 首未显示"

        await ctx.send(msg)

    async def play_next(self, channel_id: int):
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

        # ===== 本地文件 =====
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
                    await text_channel.send(f"🎵 正在播放本地文件：**{song.title}**（by {song.requester_name}）")
                self._start_predownload_task(channel_id)
            except Exception as e:
                if text_channel:
                    await text_channel.send(f"❌ 本地文件播放失败：{e}")
                await self.play_next(channel_id)
            return

        # ===== URL：优先使用已缓存文件 =====
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

    @commands.hybrid_command(
        name="shuffle",
        description="打乱当前播放队列",
        guild=discord.Object(id=server_id)
    )
    async def shuffle(self, ctx):

        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("你得先在语音频道里喵~")
            return

        session = self.sessions.get(channel.id)

        if not session or not session.queue:
            await ctx.send("队列是空的喵~")
            return

        import random

        random.shuffle(session.queue)

        await ctx.send("🔀 队列已随机打乱喵~")

    @commands.hybrid_command(
        name="play_next",
        description="跳过当前播放并播放下一首",
        guild=discord.Object(id=server_id)
    )
    async def play_next_command(self, ctx):
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

        if vc.is_playing():
            vc.stop()
            await ctx.send("⏭️ 跳过当前歌曲")
        else:
            await self.play_next(channel.id)
            await ctx.send("▶️ 当前没有播放，已尝试播放下一首")

    @commands.hybrid_command(
        name="stop",
        description="停止播放并清空当前频道的队列",
        guild=discord.Object(id=server_id)
    )
    async def stop(self, ctx):
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

        # 清理队列缓存
        for song in session.queue:
            if song.is_url and song.local_path:
                self._delete_file_safely(song.local_path)
                song.local_path = None

        # 清理当前播放缓存
        if session.current_song and session.current_song.is_url and session.current_song.local_path:
            self._delete_file_safely(session.current_song.local_path)
            session.current_song.local_path = None

        if vc and vc.is_connected():
            if vc.is_playing():
                vc.stop()
            await vc.disconnect()
            await ctx.send("🛑 已停止播放并离开频道")

            session.queue.clear()
            session.vc = None
            session.current_song = None
            self.sessions.pop(channel.id, None)
        else:
            await ctx.send("我没有连接语音频道喵~")


async def setup(bot):
    await bot.add_cog(Music(bot))
    print("✅ Music cog 已成功加载")