"""Native Discord polls: public bot-owned messages, durable ownership, no vote replay."""
import logging
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
from bot_app.domain.polls import validate_poll, poll_message_target

logger = logging.getLogger(__name__)


class Polls(commands.Cog):
    def __init__(self, bot, repository):
        self.bot = bot
        self.repository = repository

    @commands.hybrid_command(name="poll", description="创建单选或多选投票，选项用 | 分隔")
    @commands.guild_only()
    @commands.has_permissions(send_polls=True)
    @commands.bot_has_permissions(send_messages=True, send_polls=True)
    @commands.cooldown(1, 30, commands.BucketType.member)
    @app_commands.describe(question="投票问题", options="2–10 个选项，用 | 分隔，例如 苹果|香蕉|橙子",
                           hours="持续小时数，1–768，默认 24", multiple="是否允许多选，默认单选")
    async def poll(self, ctx, question: str, options: str, hours: int = 24, multiple: bool = False):
        """!poll \"晚饭吃什么\" \"火锅|披萨|面条\" 24 false"""
        try:
            values = validate_poll(question, options, hours, multiple)
        except ValueError as error:
            await ctx.send(str(error), ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        poll = discord.Poll(values["question"], timedelta(hours=hours), multiple=multiple)
        for answer in values["answers"]:
            poll.add_answer(text=answer)
        # A normal bot message can be ended through Message.end_poll; webhook polls cannot.
        message = await ctx.channel.send(content=f"投票发起人：<@{ctx.author.id}>", poll=poll,
                                         allowed_mentions=discord.AllowedMentions.none())
        record = dict(values, creator_id=ctx.author.id, channel_id=ctx.channel.id,
                      message_id=message.id, created_at=discord.utils.utcnow().isoformat(), ended=False)
        try:
            self.repository.save(ctx.guild.id, message.id, record)
        except OSError:
            logger.exception("Poll created but local ownership could not be saved: %s", message.id, extra={"guild_id": ctx.guild.id})
            await ctx.send(f"投票已创建：{message.jump_url}\n本地发起人记录保存失败；到期结算不受影响，提前结束需管理员操作。", ephemeral=True)
            return
        await ctx.send(f"投票已创建：{message.jump_url}\n{hours} 小时后自动结束；提前结束可用 /poll_end。", ephemeral=True)

    @commands.hybrid_command(name="poll_end", description="提前结束投票（发起人或管理员）")
    @commands.guild_only()
    @app_commands.describe(message="投票消息链接，或本频道内的消息 ID")
    async def poll_end(self, ctx, message: str):
        """!poll_end <投票消息链接或ID>"""
        try:
            channel_id, message_id = poll_message_target(message, ctx.guild.id, ctx.channel.id)
        except ValueError as error:
            await ctx.send(str(error), ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        channel = ctx.guild.get_channel_or_thread(channel_id)
        if channel is None:
            await ctx.send("找不到投票所在频道。", ephemeral=True)
            return
        permissions = channel.permissions_for(ctx.author)
        if not permissions.view_channel or not permissions.read_message_history:
            await ctx.send("你没有查看该投票的权限。", ephemeral=True)
            return
        admin = ctx.author.guild_permissions.manage_guild or permissions.manage_messages
        try:
            record = self.repository.get(ctx.guild.id, message_id)
        except (OSError, ValueError):
            logger.exception("Cannot read poll ownership record", extra={"guild_id": ctx.guild.id})
            record = None
        if not admin and (not record or record.get("creator_id") != ctx.author.id or record.get("channel_id") != channel_id):
            await ctx.send("只有投票发起人或管理员可以提前结束。", ephemeral=True)
            return
        try:
            target = await channel.fetch_message(message_id)
        except discord.NotFound:
            await ctx.send("投票消息已删除或 ID 不正确。", ephemeral=True)
            return
        if target.author.id != self.bot.user.id or target.poll is None:
            await ctx.send("只能结束由本 Bot 创建的投票。", ephemeral=True)
            return
        expired = target.poll.expires_at and target.poll.expires_at <= discord.utils.utcnow()
        if not target.poll.is_finalised() and not expired:
            await target.end_poll()
        if record:
            try:
                self.repository.save(ctx.guild.id, message_id, dict(record, ended=True))
            except OSError:
                logger.exception("Poll ended but local metadata update failed", extra={"guild_id": ctx.guild.id})
        await ctx.send(f"投票已结束，结果请查看原消息：{target.jump_url}", ephemeral=True)

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, (commands.BotMissingPermissions, discord.Forbidden)):
            await ctx.send("Bot 需要该频道的「发送投票」「发送消息」「查看频道」权限；结束投票还需「读取消息历史」。", ephemeral=True)
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("你没有该频道的「发送投票」权限。", ephemeral=True)
        else:
            from bot_app.presentation.discord.bot import error_message
            await ctx.send(error_message(error, ctx.guild.id), ephemeral=True)


async def setup(bot):
    from bot_app.infrastructure.paths import BASE_DIR
    from bot_app.infrastructure.persistence.polls import PollRepository
    await bot.add_cog(Polls(bot, PollRepository(BASE_DIR / "data" / "polls")))
