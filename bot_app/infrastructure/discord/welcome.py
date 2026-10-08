"""Discord channel selection, safe mention rendering and delivery."""
import logging
import discord
from bot_app.domain.welcome import render_welcome_message

logger = logging.getLogger("bot.welcome")


class WelcomeDelivery:
    def pick_channel(self, guild, channel_id):
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        channel = guild.system_channel
        return channel if isinstance(channel, discord.TextChannel) else None

    async def send(self, channel, member, reason, template=""):
        bot_member = member.guild.me
        if bot_member is None:
            logger.warning("Bot member is not cached", extra={"guild_id": member.guild.id})
            return False
        perms = channel.permissions_for(bot_member)
        if not (perms.view_channel and perms.send_messages):
            logger.warning("Missing welcome permissions", extra={"guild_id": member.guild.id, "channel_id": channel.id})
            return False
        guild_name = discord.utils.escape_markdown(member.guild.name)
        msg = render_welcome_message(template, user=member.mention, server=guild_name,
                                     count=member.guild.member_count)
        try:
            await channel.send(msg, allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[member], replied_user=False,
            ))
        except (discord.HTTPException, OSError):
            logger.warning("Failed to welcome member %s in guild %s (%s)",
                           member.id, member.guild.id, reason, exc_info=True, extra={"guild_id": member.guild.id, "channel_id": channel.id})
            return False
        return True
