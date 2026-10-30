"""Atomic JSON repository retaining the existing configuration format."""
import json
import os
import copy
from pathlib import Path
from bot_app.app.settings_catalog import DEFAULTS, validate_settings
from bot_app.shared.runtime import _atomic_json


class JsonSettingsRepository:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir

    def _path(self, section, guild_id=None):
        if section not in DEFAULTS:
            raise ValueError("不支持的设置分组")
        if guild_id is None:
            return self.config_dir / f"{section}.json"
        key = str(guild_id)
        if not key.isascii() or not key.isdigit() or not 0 < int(key) < 2**64:
            raise ValueError("无效的服务器 ID")
        return self.config_dir / "guilds" / str(int(key)) / f"{section}.json"

    def defaults(self, section):
        values = copy.deepcopy(DEFAULTS[section])
        if section == "welcome":
            for key, env in [("enabled", "WELCOME_ENABLED"), ("include_bots", "WELCOME_INCLUDE_BOTS")]:
                raw = os.getenv(env, "true").strip().lower()
                values[key] = raw not in {"0", "false", "no", "off"}
            raw = os.getenv("WELCOME_CHANNEL_ID", "0").strip()
            values["channel_id"] = raw if raw.isascii() and raw.isdigit() and int(raw) < 2**64 else "0"
        return values

    def read(self, section, guild_id=None):
        path = self._path(section, guild_id)
        defaults = self.defaults(section) if guild_id is None else self.read(section)
        if guild_id is not None and not path.exists():
            if section in {"chat", "memory"}:
                return copy.deepcopy(defaults)
            self.write(section, defaults, guild_id)
            return dict(defaults)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            # Preserve the desktop UI's fallback for older/partial files.
            if not isinstance(value, dict):
                raise ValueError("Settings must be an object")
            merged = dict(defaults, **value)
            return validate_settings(section, merged)
        except (OSError, ValueError, TypeError):
            return defaults

    def write(self, section, settings, guild_id=None):
        value = validate_settings(section, settings)
        if guild_id is not None and section in {"chat", "memory"}:
            defaults = self.read(section)
            value = {key: item for key, item in value.items() if item != defaults[key]}
        _atomic_json(self._path(section, guild_id), value)

    def any_welcome_enabled(self):
        if self.read("welcome")["enabled"]:
            return True
        return any(self.read("welcome", path.parent.name)["enabled"]
                   for path in (self.config_dir / "guilds").glob("*/welcome.json"))
