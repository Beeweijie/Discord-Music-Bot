"""Stable launcher; importing this module has no runtime side effects."""
from bot_app.main import main, run_bot, startup_problems
from bot_app.presentation.discord.bot import MyBot, create_bot

if __name__ == "__main__":
    raise SystemExit(main())
