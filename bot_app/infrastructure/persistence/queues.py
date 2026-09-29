"""Durable, per-channel queue snapshots. Audio files and live connections are not stored."""
import json
import logging
from pathlib import Path
from bot_app.infrastructure.persistence.runtime import _atomic_json

logger = logging.getLogger(__name__)


class QueueRepository:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, key):
        guild, channel = key.split(":")
        if not all(x.isascii() and x.isdigit() and 0 < int(x) < 2**64 for x in (guild, channel)):
            raise ValueError("Invalid queue identity")
        return self.root / guild / (channel + ".json")

    def save(self, key, snapshot):
        _atomic_json(self.path(key), dict(snapshot, version=1))

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def load(self):
        result = {}
        for path in self.root.glob("*/*.json"):
            try:
                key = path.parent.name + ":" + path.stem
                self.path(key)
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("Unsupported queue snapshot")
                result[key] = data
            except (OSError, ValueError):
                guild = path.parent.name
                guild = guild if guild.isascii() and guild.isdigit() and 0 < int(guild) < 2**64 else None
                logger.exception("Cannot restore queue %s; file retained", path.name, extra={"guild_id": guild})
        return result
