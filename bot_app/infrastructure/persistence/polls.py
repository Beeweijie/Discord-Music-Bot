"""Per-guild poll ownership metadata. Votes and deadlines are authoritative on Discord."""
import json
from pathlib import Path
from bot_app.infrastructure.persistence.runtime import _atomic_json


class PollRepository:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, guild_id, message_id):
        for value in (guild_id, message_id):
            if type(value) is not int or not 0 < value < 2**64:
                raise ValueError("无效的投票 ID")
        return self.root / str(guild_id) / f"{message_id}.json"

    def save(self, guild_id, message_id, record):
        _atomic_json(self._path(guild_id, message_id), record)

    def get(self, guild_id, message_id):
        path = self._path(guild_id, message_id)
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(result, dict):
            raise ValueError("投票记录损坏")
        return result
