"""Discord bot entry point; importing this module never starts or stops a bot."""

import argparse
import asyncio
import ctypes
import logging
import os
import shutil
import signal
import uuid

import discord
from dotenv import load_dotenv

from bot_app.infrastructure.paths import BASE_DIR
from bot_app.infrastructure.processes.runtime import (
    ProcessLock, is_process_record_running, read_process_record,
    remove_process_record, write_process_record,
)
from bot_app.infrastructure.persistence.runtime import write_status

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


from bot_app.presentation.discord.bot import MyBot, create_bot


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
    api = None
    try:
        if os.getenv("MANAGEMENT_API_ENABLED", "true").lower() in {"1", "true", "yes"}:
            from bot_app.presentation.api.server import start_server
            api = await start_server(
                bot.management,
                os.getenv("MANAGEMENT_API_HOST", "127.0.0.1"),
                int(os.getenv("MANAGEMENT_API_PORT", "8766")),
            )
        async with bot:
            await bot.start(token)
    finally:
        if api:
            await api.cleanup()
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
        from bot_app.infrastructure.logging_setup import configure_logging
        configure_logging(BASE_DIR / "logs")
        record = write_process_record(PID_FILE)
        write_status(reset=True, state="starting", pid=record["pid"], instance_id=uuid.uuid4().hex,
                     bot_user=None, current_song=None, queue_size=0, music_sessions={})
        if problems:
            state, last_error = "error", " ".join(problems)
            logger.error(last_error)
            return 1
        lower_windows_process_priority()
        bot = create_bot()
        try:
            asyncio.run(run_bot(bot, os.environ["DISCORD_TOKEN"].strip()))
        except discord.LoginFailure:
            state, last_error = "error", "Discord 登录失败，请检查 DISCORD_TOKEN。"
        except discord.PrivilegedIntentsRequired:
            state, last_error = "error", "请在 Discord Developer Portal 启用 Members / Message Content Intent，或在 .env 中关闭相应功能。"
        except KeyboardInterrupt:
            pass
        except Exception:
            state, last_error = "error", "启动或运行失败，请查看 logs/system.log。"
            logger.exception("Bot failed")
        if last_error:
            logger.error(last_error)
        return 1 if state == "error" else (75 if getattr(bot, "update_requested", False) else 0)
    finally:
        # Only the process that acquired ownership may erase its runtime state.
        if record and read_process_record(PID_FILE) == record:
            write_status(state=state, last_error=last_error, pid=None, current_song=None,
                         requester=None, queue_size=0, latency_ms=None, music_sessions={})
            remove_process_record(PID_FILE, record)
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
