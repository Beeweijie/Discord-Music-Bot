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
        self.queues = {}  # {channel_id: [(ctx, url), ...]}
        self.voice_clients = {}  # {channel_id: VoiceClient}

    @commands.hybrid_command(guild = discord.Object(id=server_id))
    async def play(self, ctx, input):
        """testing OvO"""
        url = input
        if not ctx.author.voice:
            await ctx.send("先加入语音啊喵~")
            return

        channel = ctx.author.voice.channel
        channel_id = channel.id

        # 初始化队列
        self.queues.setdefault(channel_id, [])
        self.queues[channel_id].append((ctx, input))
        await ctx.send(f"✅ 添加到播放队列：`{input}`")

        # 如果未连接该频道，连接并记录
        if channel_id not in self.voice_clients:
            vc = await channel.connect()
            self.voice_clients[channel_id] = vc
        else:
            vc = self.voice_clients[channel_id]

        if not vc.is_playing():
            await self.play_next(channel_id)

    async def play_next(self, channel_id):
        queue = self.queues.get(channel_id)
        if not queue:
            return

        ctx, url = queue.pop(0)
        vc = self.voice_clients[channel_id]

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

    @commands.hybrid_command(name="play_next", guild = discord.Object(id=server_id))
    async def play_next_command(self, ctx):
        """跳过当前播放并播放下一首"""
        if not ctx.author.voice:
            await ctx.send("你得先在语音频道里喵~")
            return

        channel = ctx.author.voice.channel
        channel_id = channel.id

        vc = self.voice_clients.get(channel_id)
        if not vc or not vc.is_connected():
            await ctx.send("我还没加入语音频道喵~")
            return

        if vc.is_playing():
            vc.stop()  # 触发 after 回调自动调用 play_next
            await ctx.send("⏭️ 跳过当前歌曲")
        else:
            await self.play_next(channel_id)
            await ctx.send("▶️ 当前没有播放，已尝试播放下一首")

    @commands.hybrid_command(name="stop", description="停止播放并清空当前频道的队列",
                             guild = discord.Object(id=server_id))
    async def stop(self, ctx):
        """停止当前频道播放并清空队列"""
        if not ctx.author.voice:
            await ctx.send("你得先加入语音频道喵~")
            return

        channel_id = ctx.author.voice.channel.id

        vc = self.voice_clients.get(channel_id)
        if vc and vc.is_connected():
            if vc.is_playing():
                vc.stop()  # 停止播放
            await vc.disconnect()  # 离开语音频道
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

