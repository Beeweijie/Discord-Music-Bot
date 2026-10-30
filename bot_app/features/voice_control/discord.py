"""Discord commands/listeners for local voice control."""
from discord.ext import commands
from .service import VoiceControlService

class VoiceCommands(VoiceControlService, commands.Cog):
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        await super().on_voice_state_update(member, before, after)

    @commands.hybrid_command(name="voice", description="Voice music commands: status, on, off, test, quiet")
    @commands.guild_only()
    async def voice(self, ctx, mode: str = "status"):
        if mode not in {"status", "on", "off", "test", "quiet"}:
            await ctx.send("用法：/voice status、/voice on、/voice off、/voice test、/voice quiet")
            return
        if mode == 'quiet':
            active = self.active.get(ctx.guild.id)
            if not active or not self.live_member(active[0], active[1], ctx.author.id):
                await ctx.send('请先加入喵酱正在监听的语音频道。', ephemeral=True)
                return
            self.reset_user(active[0], active[2], ctx.author.id)
            await ctx.send('🐱 已结束本次监听和候选选择。需要时再喊「喵酱喵酱」。', ephemeral=True)
            return
        if mode == "test":
            active = self.active.get(ctx.guild.id)
            if not active or not self.live_member(active[0], active[1], ctx.author.id):
                await ctx.send("请先加入喵酱正在监听的语音频道。", ephemeral=True)
                return
            self.test_users[(ctx.guild.id, ctx.author.id)] = {
                "ctx": ctx, "expires": time.monotonic() + 30, "remaining": 3,
                "baseline_frames": active[1].activity(ctx.author.id)["frames"]}
            await ctx.send("🧪 现在只说测试口令。接下来 30 秒最多显示你本人的 3 段测试转写；前缀命令的结果会显示在本频道，斜线命令仅自己可见。不保存录音，30 秒后自动结束。",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        if mode != "status":
            if not ctx.author.guild_permissions.manage_guild:
                await ctx.send("开启或关闭语音指令需要「管理服务器」权限。")
                return
            if mode == "off":
                self.disabled_guilds.add(ctx.guild.id)
                self.stop_guild(ctx.guild.id)
            else:
                self.disabled_guilds.discard(ctx.guild.id)
        state = "关闭" if ctx.guild.id in self.disabled_guilds else ("监听中" if ctx.guild.id in self.active else
            ("已就绪" if self.recognizer else "未安装或未开启"))
        await ctx.send(f"🐱 语音指令：{state}。喵酱加入语音频道后，说「喵酱喵酱，下一首歌」或「喵酱喵酱，播放＋歌名」。本地识别，不保存录音。on/off 设置保留至本次进程结束。")


async def setup(bot):
    await bot.add_cog(VoiceCommands(bot, bot.get_cog('Music').api))
