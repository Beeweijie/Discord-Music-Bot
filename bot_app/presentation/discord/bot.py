"""Discord bot entry point; importing this module never starts or stops a bot."""

import json
import logging
import math
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_app.infrastructure.paths import BASE_DIR, EMOJI_JSON
from bot_app.infrastructure.persistence.runtime import write_status

PID_FILE = BASE_DIR / "runtime" / "bot.pid"
logger = logging.getLogger(__name__)


def env_bool(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() not in {"0", "false", "no", "off"}


def error_message(error: Exception, guild_id=None) -> str:
    """Actionable public errors; internal exceptions only go to local logs."""
    error = getattr(error, "original", error)
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return "Use this command in a server channel."
    if isinstance(error, (commands.NotOwner, commands.MissingPermissions, app_commands.MissingPermissions)):
        return "You do not have permission to use this command."
    if isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions, discord.Forbidden)):
        return "Missing bot permissions. Check View Channel, Send Messages, Embed Links, Connect and Speak."
    if isinstance(error, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
        return f"Please retry in {error.retry_after:.1f} seconds."
    if isinstance(error, commands.MissingRequiredArgument):
        return f"Missing argument: {error.param.name}. Use !help for usage."
    if isinstance(error, (commands.UserInputError, app_commands.TransformerError)):
        return "Invalid arguments. Use !help or /help_music for usage."
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return "Cannot run this command here. Check the channel and permissions."
    if isinstance(error, discord.HTTPException):
        return "Discord could not process the request. Please retry later."
    logger.error("Command failed", exc_info=(type(error), error, error.__traceback__), extra={"guild_id": guild_id})
    return "Operation failed. Details are in the bot log. Please retry later."


class BotCommandTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(error_message(error, interaction.guild_id), ephemeral=True)
            else:
                await interaction.response.send_message(error_message(error, interaction.guild_id), ephemeral=True)
        except discord.HTTPException:
            logger.warning("Could not deliver slash command error", exc_info=True, extra={"guild_id": interaction.guild_id})


class MyBot(commands.Bot):
    def __init__(self):
        from bot_app.bootstrap import create_management_service
        self.management = create_management_service()
        intents = discord.Intents.default()
        intents.message_content = env_bool("MESSAGE_CONTENT_ENABLED")
        intents.members = self.management.settings.any_welcome_enabled()
        super().__init__(command_prefix="!", intents=intents, tree_cls=BotCommandTree,
                         allowed_mentions=discord.AllowedMentions.none())
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.management.catalog = self.guild_catalog

    def guild_catalog(self):
        return [{"id": str(g.id), "name": g.name,
                 "channels": [{"id": str(c.id), "name": c.name} for c in g.text_channels]}
                for g in self.guilds]

    async def setup_hook(self):
        await self.load_extension("bot_app.presentation.discord.music")
        await self.load_extension("bot_app.presentation.discord.polls")
        if self.intents.members:
            await self.load_extension("bot_app.presentation.discord.welcome")
        self.apply_saved_settings()
        self.settings_refresh.start()
        # A transient sync failure must not take working prefix commands offline.
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d global slash commands", len(synced))
        except discord.HTTPException:
            logger.exception("Slash command sync failed; the owner can retry with !sync")

    def latency_ms(self):
        return round(self.latency * 1000) if math.isfinite(self.latency) else None

    def apply_saved_settings(self):
        from bot_app.application.settings import apply_settings, guild_settings_reports
        reports = apply_settings(self, self.management.settings)
        guild_reports = guild_settings_reports(self, self.management.settings)
        if guild_reports != getattr(self, "_guild_settings_report", None):
            write_status(guild_settings_applied=guild_reports)
            self._guild_settings_report = guild_reports
        if reports != getattr(self, "_settings_report", None):
            write_status(settings_applied=reports)
            self._settings_report = reports

    @tasks.loop(seconds=2)
    async def settings_refresh(self):
        try:
            self.apply_saved_settings()
        except Exception:
            logger.exception("Could not apply saved settings")

    async def on_ready(self):
        for guild in self.guilds:
            logger.info("Server connected", extra={"guild_id": guild.id})
        music = self.get_cog("Music")
        if music:
            for session in music.service.sessions.values():
                guild = self.get_guild(session.guild_id)
                channel = self.get_channel(session.channel_id)
                session.guild_name = guild.name if guild else str(session.guild_id)
                session.channel_name = channel.name if channel else str(session.channel_id)
                music.service._write_music_status(session)
                if session.restored:
                    logger.info("Saved queue ready: %d tracks", len(session.queue), extra={"guild_id": session.guild_id, "channel_id": session.channel_id})
        logger.info("Bot online: %s", self.user)
        write_status(state="online", bot_user=str(self.user), guild_count=len(self.guilds),
                     latency_ms=self.latency_ms(), started_at=self.started_at, last_error=None)
        if not self.status_heartbeat.is_running():
            self.status_heartbeat.start()

    async def on_disconnect(self):
        if not self.is_closed():
            write_status(state="reconnecting", latency_ms=None)

    async def on_resumed(self):
        write_status(state="online", latency_ms=self.latency_ms())

    @tasks.loop(seconds=15)
    async def status_heartbeat(self):
        write_status(state="online" if self.is_ready() else "reconnecting",
                     bot_user=str(self.user) if self.user else None,
                     guild_count=len(self.guilds), latency_ms=self.latency_ms())

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if ctx.command and ctx.command.has_error_handler():
            return
        if ctx.cog and ctx.cog.has_error_handler():
            return
        try:
            await ctx.send(error_message(error, getattr(ctx.guild, "id", None)))
        except discord.HTTPException:
            logger.warning("Could not deliver command error", exc_info=True, extra={"guild_id": getattr(ctx.guild, "id", None)})

    async def close(self):
        self.settings_refresh.cancel()
        self.status_heartbeat.cancel()
        await super().close()


def create_bot() -> MyBot:
    bot = MyBot()

    @bot.command(name="sync", aliases=["a"])
    @commands.is_owner()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def sync_commands(ctx):
        """Owner only: synchronize global slash commands."""
        synced = await bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} global slash commands.")

    @bot.command()
    async def emoji(ctx):
        """Show configured emoji."""
        try:
            values = json.loads(EMOJI_JSON.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError("Emoji config must be an object")
        except (OSError, ValueError):
            await ctx.send("表情配置无法读取，请检查 config/emoji.json。")
            return
        text = " ".join(str(value) for value in values.values())
        await ctx.send(text[:1900] or "尚未配置表情。")

    @bot.command()
    async def add(ctx, a: int, b: int):
        """Add two integers."""
        await ctx.send(a + b)

    @bot.hybrid_command()
    async def ping(ctx):
        """Check the bot's Discord latency."""
        latency = bot.latency_ms()
        await ctx.send(f"Pong! {latency} ms" if latency is not None else "Connecting to Discord.")

    return bot
