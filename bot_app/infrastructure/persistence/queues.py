"""One durable queue per server, with read support for legacy channel snapshots."""
import json
import logging
import time
from pathlib import Path
from bot_app.infrastructure.persistence.runtime import _atomic_json

logger = logging.getLogger(__name__)


class QueueRepository:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, key):
        parts = key.split(":")
        if len(parts) not in (1, 2) or not all(
                x.isascii() and x.isdigit() and 0 < int(x) < 2**64 for x in parts):
            raise ValueError("Invalid queue identity")
        return self.root / parts[0] / ((parts[1] if len(parts) == 2 else "queue") + ".json")

    def save(self, key, snapshot):
        _atomic_json(self.path(key), dict(snapshot, version=1 if ":" in key else 2, saved_at=time.time()))

    def delete(self, key):
        if ":" in key:
            self.path(key).unlink(missing_ok=True)
        else:
            # Keep an empty server snapshot so old channel files cannot resurrect
            # a queue that the user explicitly stopped/cleared after migration.
            self.save(key, {"tracks": [], "history": []})

    def load(self):
        result = {}
        paths = sorted(self.root.glob("*/*.json"))
        legacy_changed = set()
        for path in paths:
            canonical = path.parent / "queue.json"
            if path.stem.isascii() and path.stem.isdigit() and canonical.exists():
                try:
                    if path.stat().st_mtime_ns > canonical.stat().st_mtime_ns:
                        legacy_changed.add(path.parent.name)
                except OSError:
                    logger.exception("Cannot compare queue snapshots", extra={"guild_id": path.parent.name})
        for path in paths:
            try:
                canonical = path.stem == "queue"
                if canonical and path.parent.name in legacy_changed:
                    continue  # An older bot ran after an automatic update rollback.
                if (not canonical and (path.parent / "queue.json").exists()
                        and path.parent.name not in legacy_changed):
                    continue  # Originals stay on disk as backups after migration.
                key = path.parent.name if canonical else path.parent.name + ":" + path.stem
                self.path(key)
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != (2 if canonical else 1):
                    raise ValueError("Unsupported queue snapshot")
                if not canonical:
                    data["channel_id"] = int(path.stem)
                data["_saved_at"] = path.stat().st_mtime
                result[key] = data
            except (OSError, ValueError):
                guild = path.parent.name
                guild = guild if guild.isascii() and guild.isdigit() and 0 < int(guild) < 2**64 else None
                logger.exception("Cannot restore queue %s; file retained", path.name, extra={"guild_id": guild})
        return result
