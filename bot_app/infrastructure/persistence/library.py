"""Small personal libraries, isolated by guild and user; atomic JSON writes."""
import json
from pathlib import Path
from bot_app.infrastructure.persistence.runtime import _atomic_json
from bot_app.domain.music.library import validate_track


class LibraryRepository:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, guild, user):
        if any(type(x) is not int or not 0 < x < 2**64 for x in (guild, user)):
            raise ValueError("Invalid library owner.")
        return self.root / str(guild) / f"{user}.json"

    def read(self, guild, user):
        path = self.path(guild, user)
        if not path.exists():
            return {"favorites": [], "playlists": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("favorites"), list) or not isinstance(data.get("playlists"), dict):
                raise ValueError()
            if len(data["favorites"]) > 500 or len(data["playlists"]) > 20:
                raise ValueError()
            for tracks in [data["favorites"], *data["playlists"].values()]:
                if not isinstance(tracks, list) or len(tracks) > 1001:
                    raise ValueError()
                for track in tracks:
                    validate_track(track)
            return data
        except (ValueError, TypeError) as error:
            raise ValueError("The library file is damaged. It has been preserved; check the log or restore a backup.") from error

    def save(self, guild, user, data):
        _atomic_json(self.path(guild, user), data)
