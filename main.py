"""Discord bot entry point; importing this module never starts or stops a bot."""

import argparse
import asyncio
import ctypes
import json
import logging
import math
import os
import shutil
import signal
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from bot.path import BASE_DIR, EMOJI_JSON
from bot.process_runtime import (
    ProcessLock, is_process_record_running, read_process_record,
    remove_process_record, write_process_record,
)
from bot.status import write_status

PID_FILE = BASE_DIR / "runtime" / "bot.pid"
logger = logging.getLogger(__name__)


def lower_windows_process_priority():
    if os.name != "nt":
        return
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000)
    except (OSError, AttributeError):
        logger.warning("Could not lower process priority", exc_info=True)


def env_bool(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() not in {"0", "false", "no", "off"}


def error_message(error: Exception) -> str:
    """Actionable public errors; internal exceptions only go to local logs."""
    error = getattr(error, "original", error)
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return "请在服务器频道中使用此命令。"
    if isinstance(error, (commands.NotOwner, commands.MissingPermissions, app_commands.MissingPermissions)):
        return "你没有使用此命令的权限。"
    if isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions, discord.Forbidden)):
        return "机器人缺少所需权限，请检查频道的查看、发送消息、嵌入链接、连接和说话权限。"
    if isinstance(error, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
        return f"操作太快，请在 {error.retry_after:.1f} 秒后重试。"
    if isinstance(error, commands.MissingRequiredArgument):
        return f"缺少参数：{error.param.name}。使用 !help 查看用法。"
    if isinstance(error, (commands.UserInputError, app_commands.TransformerError)):
        return "参数格式不正确，请使用 !help 或 /help_music 查看用法。"
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return "当前无法执行此命令，请检查频道和权限。"
    if isinstance(error, discord.HTTPException):
        return "Discord 暂时无法处理请求，请稍后重试。"
    logger.error("Command failed", exc_info=(type(error), error, error.__traceback__))
    return "操作失败，详情已记录到机器人日志。请稍后重试。"


class BotCommandTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(error_message(error), ephemeral=True)
            else:
                await interaction.response.send_message(error_message(error), ephemeral=True)
        except discord.HTTPException:
            logger.warning("Could not deliver slash command error", exc_info=True)


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = env_bool("MESSAGE_CONTENT_ENABLED")
        intents.members = env_bool("WELCOME_ENABLED")
        super().__init__(command_prefix="!", intents=intents, tree_cls=BotCommandTree,
                         allowed_mentions=discord.AllowedMentions.none())
        self.started_at = datetime.now(timezone.utc).isoformat()

    async def setup_hook(self):
        await self.load_extension("bot.music")
        if self.intents.members:
            await self.load_extension("bot.welcome")
        # A transient sync failure must not take working prefix commands offline.
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d global slash commands", len(synced))
        except discord.HTTPException:
            logger.exception("Slash command sync failed; the owner can retry with !sync")

    def latency_ms(self):
        return round(self.latency * 1000) if math.isfinite(self.latency) else None

    async def on_ready(self):
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
            await ctx.send(error_message(error))
        except discord.HTTPException:
            logger.warning("Could not deliver command error", exc_info=True)

    async def close(self):
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
        await ctx.send(f"已同步 {len(synced)} 个全局 slash 命令。")

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
        await ctx.send(f"Pong! {latency} ms" if latency is not None else "正在连接 Discord。")

    return bot


def startup_problems() -> list[str]:
    """Check configuration without logging in or displaying the token."""
    problems = []
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token.lower().startswith(("your_", "replace_")):
        problems.append("请在项目 .env 中设置有效的 DISCORD_TOKEN。")
    executable = os.getenv("FFMPEG_PATH", "").strip() or shutil.which("ffmpeg")
    if not executable:
        fallback = BASE_DIR.__class__("C:/Program Files/ffmpeg/bin/ffmpeg.exe")
        executable = str(fallback) if fallback.is_file() else None
    if not executable or not (shutil.which(executable) or BASE_DIR.__class__(executable).is_file()):
        problems.append("未找到 FFmpeg，请安装 FFmpeg 或设置 FFMPEG_PATH。")
    return problems


async def run_bot(bot: MyBot, token: str):
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGTERM)
    def shutdown(_signum, _frame):
        loop.call_soon_threadsafe(lambda: asyncio.create_task(bot.close()))
    signal.signal(signal.SIGTERM, shutdown)
    try:
        async with bot:
            await bot.start(token)
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Discord Music Bot")
    parser.add_argument("--check", action="store_true", help="Validate configuration without connecting")
    args = parser.parse_args(argv)
    load_dotenv(BASE_DIR / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    problems = startup_problems()
    if args.check:
        for problem in problems:
            logger.error(problem)
        if not problems:
            logger.info("Configuration and FFmpeg checks passed; no Discord connection was made.")
        return 1 if problems else 0

    lock = ProcessLock(PID_FILE.with_suffix(".lock"))
    if not lock.acquire():
        logger.info("Discord Music Bot is already running.")
        return 0
    record = None
    state, last_error = "offline", None
    try:
        if is_process_record_running(read_process_record(PID_FILE)):
            logger.info("Discord Music Bot is already running.")
            return 0
        record = write_process_record(PID_FILE)
        write_status(reset=True, state="starting", pid=record["pid"], instance_id=uuid.uuid4().hex,
                     bot_user=None, current_song=None, queue_size=0, music_sessions={})
        if problems:
            state, last_error = "error", " ".join(problems)
            logger.error(last_error)
            return 1
        lower_windows_process_priority()
        try:
            asyncio.run(run_bot(create_bot(), os.environ["DISCORD_TOKEN"].strip()))
        except discord.LoginFailure:
            state, last_error = "error", "Discord 登录失败，请检查 DISCORD_TOKEN。"
        except discord.PrivilegedIntentsRequired:
            state, last_error = "error", "请在 Discord Developer Portal 启用 Members / Message Content Intent，或在 .env 中关闭相应功能。"
        except KeyboardInterrupt:
            pass
        except Exception:
            state, last_error = "error", "启动或运行失败，请查看 logs/bot.log。"
            logger.exception("Bot failed")
        if last_error:
            logger.error(last_error)
        return 1 if state == "error" else 0
    finally:
        # Only the process that acquired ownership may erase its runtime state.
        if record and read_process_record(PID_FILE) == record:
            write_status(state=state, last_error=last_error, pid=None, current_song=None,
                         requester=None, queue_size=0, latency_ms=None, music_sessions={})
            remove_process_record(PID_FILE, record)
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
