import discord
from discord.ext import commands

# ====== 配置：欢迎频道 ID（和 music 里 server_id 类似）======
WELCOME_CHANNEL_ID = 0  # ← 换成你的欢迎频道 ID


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _pick_channel(self, guild: discord.Guild):
        """
        选择欢迎消息频道：
        1. 指定的 WELCOME_CHANNEL_ID
        2. fallback 到 system_channel
        """
        channel = guild.get_channel(WELCOME_CHANNEL_ID)
        if channel is not None:
            return channel
        return guild.system_channel

    async def _send_welcome(self, member: discord.Member, reason: str = ""):
        """
        实际发送欢迎消息的统一入口
        """
        channel = self._pick_channel(member.guild)
        if channel is None:
            print(f"[WELCOME] No channel for guild {member.guild.name}")
            return

        # 权限检查（避免 silent fail）
        if isinstance(channel, discord.TextChannel):
            perms = channel.permissions_for(member.guild.me)
            if not (perms.view_channel and perms.send_messages):
                print(f"[WELCOME] Missing perms in #{channel.name}")
                return

        msg = (
            f"欢迎 {member.mention} 加入 **{member.guild.name}**！🎉\n"
            f"你是第 **{member.guild.member_count}** 位成员。\n"
        )
        if reason:
            msg += f"（触发：{reason}）"

        await channel.send(msg)

    # ====== 事件监听 ======

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """
        成员刚加入服务器时触发
        """
        pending = getattr(member, "pending", None)
        print(f"[WELCOME] on_member_join: {member} pending={pending}")

        # 如果没有入服验证，直接欢迎
        if pending is False or pending is None:
            await self._send_welcome(member, reason="on_member_join")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """
        处理入服验证（pending: True -> False）
        """
        b = getattr(before, "pending", None)
        a = getattr(after, "pending", None)

        if b is True and a is False:
            print(f"[WELCOME] screening passed: {after}")
            await self._send_welcome(after, reason="pending->False")


# ====== Extension 入口（和 music.py 一样）======

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
    print("✅ Welcome cog 已成功加载")
