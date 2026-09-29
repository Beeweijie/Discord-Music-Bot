"""Apply saved snapshots on the owning event loop; acknowledge only actual adoption."""
from bot_app.domain.settings import revision


def apply_settings(bot, repository):
    reports = {}
    music = repository.read("music")
    cog = bot.get_cog("Music")
    if cog:
        cog.service.settings = dict(music)
        cog.service.settings_repository = repository
        reports["music"] = {"revision": revision(music), "reason": "Loaded; changes apply at the documented time for each setting."}
    welcome = repository.read("welcome")
    cog = bot.get_cog("Welcome")
    if cog:
        cog.settings_repository = repository
        cog.enabled = welcome["enabled"]
        cog.include_bots = welcome["include_bots"]
        cog.channel_id = int(welcome["channel_id"])
    needs_restart = welcome["enabled"] and (not bot.intents.members or cog is None)
    reports["welcome"] = {
        "revision": None if needs_restart else revision(welcome),
        "saved_revision": revision(welcome), "requires_restart": needs_restart,
        "reason": "Restart the bot and enable Members Intent in the Discord developer portal." if needs_restart else "New member events will use these settings.",
    }
    reports["voice_moderation"] = {"revision": None, "reason": "Stored only; voice moderation is not connected."}
    return reports


def guild_settings_reports(bot, repository):
    reports = {}
    for guild in bot.guilds:
        sections = {}
        for section in ("music", "welcome", "voice_moderation"):
            settings = repository.read(section, guild.id)
            restart = section == "welcome" and settings["enabled"] and (not bot.intents.members or bot.get_cog("Welcome") is None)
            available = section != "voice_moderation" and not restart
            sections[section] = {"revision": revision(settings) if available else None,
                "saved_revision": revision(settings), "requires_restart": restart,
                "reason": "Restart the bot to enable welcome messages." if restart else "Stored only; voice moderation is not connected." if section == "voice_moderation" else "Saved; future operations in this server will use these settings."}
        reports[str(guild.id)] = sections
    return reports
