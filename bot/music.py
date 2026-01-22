import discord
from discord.ext import commands
import yt_dlp
import os
import re
from bot.path import MUSIC_DIR

server_id = 1298956383819010090


def is_valid_url(url: str) -> bool:
    regex = re.compile(
        r'^(https?:\/\/)'
        r'(([a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,})'
        r'(\/[^\s]*)?$'
    )
    return re.match(regex, url) is not None


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}          # {channel_id: [(ctx, input), ...]}
        self.voice_clients = {}   # {channel_id: VoiceClient}

    # ✅ 新增：join 命令（/join 和 !join 都可用）
    @commands.hybrid_command(name="join", description="让 bot 加入你所在的语音频道",
                             guild=discord.Object(id=server_id))
    async def join(self, ctx):
        if not ctx.author.voice:
            await ctx.send("先加入语音啊喵~")
            return

        channel = ctx.author.voice.channel
        channel_id = channel.id

        # 如果记录里有 vc 但已经失效（常见：被踢出后残留），先清掉
        vc = self.voice_clients.get(channel_id)
        if vc and not vc.is_connected():
            self.voice_clients.pop(channel_id, None)
            vc = None

        # 如果 bot 当前已经在语音里（可能在别的频道），优先用 guild.voice_client
        guild_vc = ctx.guild.voice_client
        if guild_vc and guild_vc.is_connected():
            # 如果已经在目标频道
            if guild_vc.channel and guild_vc.channel.id == channel_id:
                self.voice_clients[channel_id] = guild_vc
                await ctx.send(f"✅ 我已经在：**{channel.name}** 里啦喵~")
                return
            # 否则尝试移动过去（比断开再连更稳）
            try:
                await guild_vc.move_to(channel)
                # 清理旧映射：把之前记录的所有 vc 都删掉（避免残留）
                for cid in list(self.voice_clients.keys()):
                    if self.voice_clients.get(cid) == guild_vc:
                        self.voice_clients.pop(cid, None)
                self.voice_clients[channel_id] = guild_vc
                await ctx.send(f"✅ 我移动到：**{channel.name}** 了喵~")
                return
            except Exception:
                # move 失败就强制断开，走下面 connect
                try:
                    await guild_vc.disconnect(force=True)
                except Exception:
                    pass

        # 还没连接就直接连
        if not vc:
            try:
                vc = await channel.connect()
                self.voice_clients[channel_id] = vc
            except Exception as e:
                await ctx.send(f"❌ 加入语音失败：{e}")
                return

        await ctx.send(f"✅ 我来啦！已加入：**{channel.name}**")

    @commands.hybrid_command(guild=discord.Object(id=server_id))
    async def play(self, ctx, input):
        """testing OvO"""
        if not ctx.author.voice:
            await ctx.send("先加入语音啊喵~")
            return

        channel = ctx.author.voice.channel
        channel_id = channel.id

        # 初始化队列
        self.queues.setdefault(channel_id, [])
        self.queues[channel_id].append((ctx, input))
        await ctx.send(f"✅ 添加到播放队列：`{input}`")

        # ✅ 最小修复：如果记录里有 vc 但已经断开（常见：被踢），清掉它再重连
        vc = self.voice_clients.get(channel_id)
        if vc and not vc.is_connected():
            self.voice_clients.pop(channel_id, None)
            vc = None

        # ✅ 如果 bot 其实已经有 guild 级别的 voice_client（可能被移动到别的频道）
        guild_vc = ctx.guild.voice_client
        if guild_vc and guild_vc.is_connected():
            # 如果 guild_vc 不在目标频道，尝试移动过去
            if guild_vc.channel and guild_vc.channel.id != channel_id:
                try:
                    await guild_vc.move_to(channel)
                except Exception:
                    try:
                        await guild_vc.disconnect(force=True)
                    except Exception:
                        pass
                    guild_vc = None
            if guild_vc and guild_vc.is_connected():
                vc = guild_vc
                # 清掉旧映射，重建映射到当前频道
                for cid in list(self.voice_clients.keys()):
                    if self.voice_clients.get(cid) == vc:
                        self.voice_clients.pop(cid, None)
                self.voice_clients[channel_id] = vc

        # 如果还没有可用连接，再 connect
        if not vc:
            try:
                vc = await channel.connect()
                self.voice_clients[channel_id] = vc
            except Exception as e:
                await ctx.send(f"❌ 连接语音失败：{e}")
                return

        if not vc.is_playing():
            await self.play_next(channel_id)

    async def play_next(self, channel_id):
        queue = self.queues.get(channel_id)
        if not queue:
            return

        # ✅ 取出下一首
        ctx, url = queue.pop(0)

        # ✅ 最小修复：vc 可能已失效（被踢/断开），先检查
        vc = self.voice_clients.get(channel_id)
        if not vc or not vc.is_connected():
            self.voice_clients.pop(channel_id, None)
            await ctx.send("我现在不在语音里喵~ 先用 /join 再 /play 继续吧")
            return

        # 本地文件播放
        if not is_valid_url(url):
            if not url.endswith(".mp3"):
                url += ".mp3"
            file_path = MUSIC_DIR / url
            if not file_path.exists():
                await ctx.send(f"❌ 找不到文件：`{url}`")
                await ctx.channel.send("发URL啊喵~")
                await ctx.channel.send("<:shock:1367501766236831806>")
                return

            ffmpeg_options = {'options': '-vn -af "volume=0.1"'}
            vc.play(
                discord.FFmpegPCMAudio(str(file_path), **ffmpeg_options),
                after=lambda e: self.bot.loop.create_task(self.play_next(channel_id))
            )
            await ctx.send(f"🎵 正在播放本地文件：`{url}`")
            return

        # 网络音频播放
        await ctx.send("🎧 正在解析链接，请稍候...")
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'forceurl': True,
            'forcejson': True,
            'extract_flat': False,
            'noplaylist': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                print_formats(info)
                audio_url = info['url']
                title = info.get('title', '未知标题')

            ffmpeg_path = "C:/Program Files/ffmpeg/bin/ffmpeg.exe"
            ffmpeg_options = {
                'before_options': (
                    '-headers "Referer: https://www.bilibili.com/\r\n'
                    'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"'
                ),
                'options': '-vn -af "volume=0.1"'
            }

            vc.play(
                discord.FFmpegPCMAudio(audio_url, executable=ffmpeg_path, **ffmpeg_options),
                after=lambda e: self.bot.loop.create_task(self.play_next(channel_id))
            )
            await ctx.send(f"🎶 正在播放：**{title}**")

        except Exception as e:
            await ctx.send(f"❌ 播放失败：{e}")
            await self.play_next(channel_id)  # 跳过错误，继续下一个

    @commands.hybrid_command(name="play_next", guild=discord.Object(id=server_id))
    async def play_next_command(self, ctx):
        """跳过当前播放并播放下一首"""
        if not ctx.author.voice:
            await ctx.send("你得先在语音频道里喵~")
            return

        channel = ctx.author.voice.channel
        channel_id = channel.id

        vc = self.voice_clients.get(channel_id)
        if not vc or not vc.is_connected():
            await ctx.send("我还没加入语音频道喵~ 先 /join 喵~")
            return

        if vc.is_playing():
            vc.stop()  # 触发 after 回调自动调用 play_next
            await ctx.send("⏭️ 跳过当前歌曲")
        else:
            await self.play_next(channel_id)
            await ctx.send("▶️ 当前没有播放，已尝试播放下一首")

    @commands.hybrid_command(name="stop", description="停止播放并清空当前频道的队列",
                             guild=discord.Object(id=server_id))
    async def stop(self, ctx):
        """停止当前频道播放并清空队列"""
        if not ctx.author.voice:
            await ctx.send("你得先加入语音频道喵~")
            return

        channel_id = ctx.author.voice.channel.id

        vc = self.voice_clients.get(channel_id)
        if vc and vc.is_connected():
            if vc.is_playing():
                vc.stop()
            await vc.disconnect()
            await ctx.send("🛑 已停止播放并离开频道")

            # 清空该频道的队列和连接记录
            self.queues.pop(channel_id, None)
            self.voice_clients.pop(channel_id, None)
        else:
            await ctx.send("我没有连接语音频道喵~")


async def setup(bot):
    await bot.add_cog(Music(bot))
    print("✅ Music cog 已成功加载")


def print_formats(info):
    formats = info.get("formats", [])
    for f in formats:
        format_id = f.get("format_id", "N/A")
        ext = f.get("ext", "N/A")
        acodec = f.get("acodec", "N/A")
        vcodec = f.get("vcodec", "N/A")
        abr = f.get("abr", "N/A")
        filesize = f.get("filesize", 0)
        is_audio = vcodec == "none"
        tag = "[AUDIO]" if is_audio else "[VIDEO]"
        print(f"{tag} [{format_id}] ext={ext} | abr={abr} | acodec={acodec} | vcodec={vcodec} | size={filesize}")
