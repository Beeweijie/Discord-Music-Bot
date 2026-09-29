"""Management interface shared by the desktop UI and local HTTP API.

Controls are acknowledged by ID, then consumed on the bot's event loop.
Submitting a command is not the same as successful execution.
"""
from bot_app.application.ports import LogReader, RuntimeRepository, SettingsRepository
from bot_app.domain.control import validate_control
from bot_app.domain.settings import DEFAULTS, EFFECTS, revision, validate_settings


class ManagementService:
    def __init__(self, runtime: RuntimeRepository, settings: SettingsRepository, logs: LogReader):
        self.runtime = runtime
        self.settings = settings
        self.logs = logs
        self.catalog = None

    def get_guilds(self):
        return self.catalog() if self.catalog else []

    def get_status(self) -> dict:
        return self.runtime.read_status()

    def get_sessions(self) -> dict:
        return self.get_status().get("music_sessions", {})

    def get_control_result(self, command_id: str):
        result = self.runtime.read_control_result(command_id)
        if result is None:
            # A still-running pre-refactor bot only publishes last_control.
            last = self.get_status().get("last_control")
            if isinstance(last, dict) and last.get("id") == command_id:
                return last
        return result

    def submit_control(self, command: dict) -> str:
        payload = validate_control(command)
        state = self.get_status()
        if not state.get("instance_id") or not state.get("pid"):
            raise ValueError("The bot is not running. Start it first.")
        payload["instance_id"] = state["instance_id"]
        return self.runtime.enqueue_control(payload)

    def get_settings(self, section: str, guild_id=None) -> dict:
        return self.settings.read(section, guild_id)

    def update_settings(self, section: str, value: dict, guild_id=None) -> dict:
        settings = validate_settings(section, value)
        if section == "welcome" and guild_id is not None and settings["channel_id"] != "0":
            guild = next((g for g in self.get_guilds() if g["id"] == str(guild_id)), None)
            if guild and settings["channel_id"] not in {c["id"] for c in guild["channels"]}:
                raise ValueError("The welcome channel must belong to the selected server.")
        self.settings.write(section, settings, guild_id)
        return settings

    def settings_report(self, section, guild_id=None):
        settings = self.get_settings(section, guild_id)
        saved_revision = revision(settings)
        applied = self.get_status().get("settings_applied", {}).get(section, {})
        if guild_id is not None:
            applied = self.get_status().get("guild_settings_applied", {}).get(str(guild_id), {}).get(section, {})
        is_current = applied.get("revision") == saved_revision
        return {"settings": settings, "saved_revision": saved_revision,
                "applied_revision": applied.get("revision"), "applied": is_current,
                "requires_restart": bool(applied.get("requires_restart")) if is_current or applied.get("saved_revision") == saved_revision else False,
                "effect": "stored_only" if section == "voice_moderation" else "runtime",
                "effects": EFFECTS[section], "reason": applied.get("reason", "Waiting for the bot to apply settings.")}

    def all_settings(self, guild_id=None):
        return {section: self.settings_report(section, guild_id) for section in DEFAULTS}

    def get_logs(self, limit: int = 80, guild_id=None, level="INFO", details=False) -> list[str]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Log limit must be between 1 and 500.")
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"} or type(details) is not bool:
            raise ValueError("Invalid log filter")
        return self.logs.read(limit, guild_id=guild_id, level=level, details=details)
