"""新成员欢迎功能。

监听成员加入服务器、入服验证完成等事件，并向指定频道发送欢迎消息。
"""

import logging
import os
from collections import OrderedDict

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)
MAX_REMEMBERED_WELCOMES = 10_000


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    logger.warning("Invalid %s; using default %s", name, default)
    return default


def _welcome_channel_id() -> int:
    try:
        channel_id = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
        if channel_id >= 0:
            return channel_id
    except ValueError:
        pass
    logger.warning("Invalid WELCOME_CHANNEL_ID; using the server system channel")
    return 0


class Welcome(commands.Cog):
    """处理 Discord 成员加入相关事件。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = _env_bool("WELCOME_ENABLED", True)
        self.include_bots = _env_bool("WELCOME_INCLUDE_BOTS", True)
        self.channel_id = _welcome_channel_id()
        # Discord can deliver overlapping join/screening events. Mark a welcome
        # before awaiting its send, and remember only successful sends.
        self._sending = set()
        self._welcomed = OrderedDict()

    # ===== 内部工具 =====

    def _pick_channel(self, guild: discord.Guild):
        """选择欢迎消息发送频道。"""
        channel = guild.get_channel(self.channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        channel = guild.system_channel
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _send_welcome(self, member: discord.Member, reason: str = ""):
        """统一的欢迎消息发送入口。"""
        if not self.enabled or (member.bot and not self.include_bots):
            return

        member_key = (member.guild.id, member.id)
        joined_at = member.joined_at
        event_key = (*member_key, joined_at)
        if event_key in self._sending or (
            member_key in self._welcomed and self._welcomed[member_key] == joined_at
        ):
            return

        channel = self._pick_channel(member.guild)
        if channel is None:
            logger.info("No welcome text channel for guild %s", member.guild.id)
            return

        # 发送前先检查权限，避免 Discord 静默失败。
        bot_member = member.guild.me
        if bot_member is None:
            logger.warning("Bot member is not cached for guild %s", member.guild.id)
            return
        perms = channel.permissions_for(bot_member)
        if not (perms.view_channel and perms.send_messages):
            logger.warning("Missing welcome permissions in channel %s", channel.id)
            return

        guild_name = discord.utils.escape_markdown(member.guild.name)
        msg = f"欢迎 {member.mention} 加入 **{guild_name}**！🎉"
        if member.guild.member_count is not None:
            msg += f"\n你是第 **{member.guild.member_count}** 位成员。"

        self._sending.add(event_key)
        try:
            await channel.send(
                msg,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[member], replied_user=False,
                ),
            )
        except (discord.HTTPException, OSError):
            logger.warning(
                "Failed to welcome member %s in guild %s (%s)",
                member.id, member.guild.id, reason, exc_info=True,
            )
        else:
            self._welcomed[member_key] = joined_at
            self._welcomed.move_to_end(member_key)
            while len(self._welcomed) > MAX_REMEMBERED_WELCOMES:
                self._welcomed.popitem(last=False)
        finally:
            self._sending.discard(event_key)

    # ===== 事件监听 =====

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """成员刚加入服务器时触发。"""
        pending = getattr(member, "pending", None)
        # 如果服务器没有开启入服验证，或者状态不可用，就直接欢迎。
        if pending is False or pending is None:
            await self._send_welcome(member, reason="on_member_join")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """处理入服验证完成：pending 从 True 变为 False。"""
        b = getattr(before, "pending", None)
        a = getattr(after, "pending", None)

        if b is True and a is False:
            await self._send_welcome(after, reason="pending->False")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Allow a member who leaves and rejoins to be welcomed again."""
        key = (member.guild.id, member.id)
        if key in self._welcomed and self._welcomed[key] == member.joined_at:
            self._welcomed.pop(key, None)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        for key in list(self._welcomed):
            if key[0] == guild.id:
                self._welcomed.pop(key, None)


# ===== Extension 入口 =====

async def setup(bot: commands.Bot):
    """discord.py 加载扩展时调用。"""
    await bot.add_cog(Welcome(bot))
    logger.info("Welcome cog loaded")
