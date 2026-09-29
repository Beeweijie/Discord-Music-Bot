"""Bounded log reads, shared by both management interfaces."""
import os
import re
import logging
from pathlib import Path
from bot_app.infrastructure.logging_setup import log_path


class FileLogReader:
    def __init__(self, path: Path):
        self.path = path

    def read(self, limit, guild_id=None, level="INFO", details=False):
        path = log_path(self.path.parent, guild_id, details) if guild_id is not None or details else self.path
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                start = max(0, stream.tell() - 256 * 1024)
                stream.seek(start)
                if start:
                    stream.readline()  # Discard a partial UTF-8 line at the boundary.
                data = stream.read()
        except FileNotFoundError:
            return []
        lines = data.decode("utf-8", errors="replace").splitlines()
        result, include = [], level in {"DEBUG", "INFO"}
        for line in lines:
            match = re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|", line)
            if match:
                include = getattr(logging, match[1]) >= getattr(logging, level)
            if include:
                result.append(line)
        return result[-limit:]
