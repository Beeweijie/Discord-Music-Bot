"""Local music/cache datasource. Business decisions about in-use files stay in the service."""
from pathlib import Path


class MusicFiles:
    def __init__(self, music_dir):
        self.music_dir = Path(music_dir)
        self.cache_dir = self.music_dir / "cache"

    def is_cache_path(self, path):
        try:
            return path.resolve().is_relative_to(self.cache_dir.resolve())
        except (OSError, ValueError):
            return False

    def cache_files(self):
        return [p for p in self.cache_dir.iterdir() if p.is_file()]

    def delete_cache_file(self, path):
        if not self.is_cache_path(path):
            raise ValueError("Refusing to delete a file outside the media cache")
        path.unlink(missing_ok=True)

    def exists(self, path):
        return path.is_file()

    def modified_at(self, path):
        return path.stat().st_mtime
