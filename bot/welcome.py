"""新成员欢迎功能。

监听成员加入服务器、入服验证完成等事件，并向指定频道发送欢迎消息。
"""

import discord
from discord.ext import commands


# ===== 配置 =====

# 欢迎频道 ID。保持 0 时，会回退到服务器的 system_channel。
WELCOME_CHANNEL_ID = 0


class Welcome(commands.Cog):
    """处理 Discord 成员加入相关事件。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ===== 内部工具 =====

    def _pick_channel(self, guild: discord.Guild):
        """选择欢迎消息发送频道。"""
        channel = guild.get_channel(WELCOME_CHANNEL_ID)
        if channel is not None:
            return channel
        return guild.system_channel

    async def _send_welcome(self, member: discord.Member, reason: str = ""):
        """统一的欢迎消息发送入口。"""
        channel = self._pick_channel(member.guild)
        if channel is None:
            print(f"[WELCOME] No channel for guild {member.guild.name}")
            return

        # 发送前先检查权限，避免 Discord 静默失败。
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

    # ===== 事件监听 =====

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """成员刚加入服务器时触发。"""
        pending = getattr(member, "pending", None)
        print(f"[WELCOME] on_member_join: {member} pending={pending}")

        # 如果服务器没有开启入服验证，或者状态不可用，就直接欢迎。
        if pending is False or pending is None:
            await self._send_welcome(member, reason="on_member_join")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """处理入服验证完成：pending 从 True 变为 False。"""
        b = getattr(before, "pending", None)
        a = getattr(after, "pending", None)

        if b is True and a is False:
            print(f"[WELCOME] screening passed: {after}")
            await self._send_welcome(after, reason="pending->False")


# ===== Extension 入口 =====

async def setup(bot: commands.Bot):
    """discord.py 加载扩展时调用。"""
    await bot.add_cog(Welcome(bot))
    print("✅ Welcome cog 已成功加载")
