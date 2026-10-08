"""新成员欢迎功能。

监听成员加入服务器、入服验证完成等事件，并向指定频道发送欢迎消息。
"""

import logging
import os

import discord
from discord.ext import commands
from bot_app.domain.welcome import DEFAULT_WELCOME_MESSAGE, DEFAULT_MEMBER_COUNT_MESSAGE


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

    def _require_welcome_channel(self, guild, channel):
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Use !welcome in a server text channel to select a welcome channel.")
        perms = channel.permissions_for(guild.me) if guild.me else None
        if not perms or not (perms.view_channel and perms.send_messages):
            raise ValueError("I need View Channel and Send Messages permissions in the welcome channel.")
        return channel

    def _configured_welcome_channel(self, guild, settings):
        channel_id = int(settings["channel_id"])
        channel = guild.get_channel(channel_id) if channel_id else guild.system_channel
        return self._require_welcome_channel(guild, channel)

    @commands.command(name="welcome")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def welcome_prefix(self, ctx, *, content: str = ""):
        """Configure welcomes (Manage Server required).

        No text: use this channel and enable. Text: also save a custom message.
        on/off: enable/disable; status: show settings; reset: default message;
        test: send a preview to the configured channel using you as the member.
        Placeholders: {user}, {server}, {count}.
        """
        management = self.bot.management
        settings = management.get_settings("welcome", ctx.guild.id)
        content = content.strip()
        action = content.casefold()
        try:
            if action == "status":
                channel_id = int(settings["channel_id"])
                channel = ctx.guild.get_channel(channel_id) if channel_id else ctx.guild.system_channel
                target = f"<#{channel.id}>" if channel else "Unavailable; use !welcome in a text channel."
                template = settings["message"] or DEFAULT_WELCOME_MESSAGE + "\n" + DEFAULT_MEMBER_COUNT_MESSAGE
                embed = discord.Embed(title="Welcome settings", description=template)
                embed.add_field(name="Status", value="Enabled" if settings["enabled"] else "Disabled")
                embed.add_field(name="Channel", value=target)
                embed.add_field(name="Message", value="Custom" if settings["message"] else "Default")
                embed.set_footer(text="Placeholders: {user}, {server}, {count}. Use !welcome test to preview.")
                await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return
            if action == "test":
                channel = self._configured_welcome_channel(ctx.guild, settings)
                if await self.delivery.send(channel, ctx.author, "command_test", settings["message"]):
                    await ctx.send(f"Test welcome sent to <#{channel.id}>. Settings are unchanged.")
                else:
                    await ctx.send("Could not send the test welcome. Check channel permissions and retry.")
                return
            if action == "off":
                settings["enabled"] = False
                reply = "Welcome messages disabled. Channel and message are kept."
            elif action == "reset":
                settings["message"] = ""
                reply = "Default welcome message restored. Channel and enabled state are unchanged."
            elif action == "on":
                channel = self._configured_welcome_channel(ctx.guild, settings)
                settings["enabled"] = True
                reply = f"Welcome messages enabled in <#{channel.id}>."
            else:
                channel = self._require_welcome_channel(ctx.guild, ctx.channel)
                settings.update(enabled=True, channel_id=str(channel.id))
                if content:
                    settings["message"] = content
                reply = f"Welcome channel set to <#{channel.id}> and enabled. "
                reply += "Custom message saved." if content else "Existing message kept."
            management.update_settings("welcome", settings, ctx.guild.id)
            self.settings_repository = management.settings
            if settings["enabled"] and not self.bot.intents.members:
                reply += " Restart the bot to receive member events; Server Members Intent must be enabled in the Discord developer portal."
            await ctx.send(reply)
        except ValueError as error:
            await ctx.send(str(error))

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
    cog = Welcome(bot)
    cog.settings_repository = bot.management.settings
    await bot.add_cog(cog)
