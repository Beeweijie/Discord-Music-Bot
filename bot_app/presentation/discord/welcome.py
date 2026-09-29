"""新成员欢迎功能。

监听成员加入服务器、入服验证完成等事件，并向指定频道发送欢迎消息。
"""

import logging
import os

from discord.ext import commands


logger = logging.getLogger("bot.welcome")
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


from bot_app.application.welcome.service import WelcomeService


class Welcome(WelcomeService, commands.Cog):
    def __init__(self, bot):
        from bot_app.bootstrap import create_welcome_delivery
        self.bot = bot
        super().__init__(create_welcome_delivery(), enabled=_env_bool("WELCOME_ENABLED", True),
                         include_bots=_env_bool("WELCOME_INCLUDE_BOTS", True),
                         channel_id=_welcome_channel_id(), max_remembered=lambda: MAX_REMEMBERED_WELCOMES)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await super().on_member_join(member)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        await super().on_member_update(before, after)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await super().on_member_remove(member)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        await super().on_guild_remove(guild)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
