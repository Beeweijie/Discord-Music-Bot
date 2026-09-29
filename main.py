"""Stable launcher; importing this module has no runtime side effects."""
if __name__ == "__main__":
    from scripts.run_bot import main
    raise SystemExit(main())
else:
    from bot_app.main import main, run_bot, startup_problems
    from bot_app.presentation.discord.bot import MyBot, create_bot
