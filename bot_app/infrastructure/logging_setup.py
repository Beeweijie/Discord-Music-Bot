"""Rotating, guild-isolated activity logs and opt-in diagnostic detail."""
import copy
import html
import logging
import re
import time
from collections import OrderedDict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from bot_app.application.diagnostics import log_identity

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def clean_text(value):
    text = ANSI.sub("", html.unescape(str(value)))
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def log_path(root, guild_id=None, details=False):
    root = Path(root)
    if guild_id is None:
        return root / ("system.debug.log" if details else "system.log")
    value = str(guild_id)
    if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**64:
        raise ValueError("Invalid server ID")
    return root / "guilds" / str(int(value)) / ("debug.log" if details else "activity.log")


class ActivityFormatter(logging.Formatter):
    def format(self, record):
        text = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            text += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
        text = " ".join(clean_text(text).split())
        if len(text) > 450:
            text = text[:447] + "..."
        component = record.name.rsplit(".", 1)[-1]
        channel = f" channel={record.channel_id}" if getattr(record, "channel_id", None) else ""
        return f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} | {record.levelname:<7} | {component}{channel} | {text}"


class DetailFormatter(logging.Formatter):
    def format(self, record):
        # Formatter caches exception text on the record; isolate it from other outputs.
        record = copy.copy(record)
        record.exc_text = None
        return clean_text(super().format(record))


class ScopedLogHandler(logging.Handler):
    """One route per record. Bound open files and repeated-summary bookkeeping."""
    def __init__(self, root, *, max_bytes=2 * 1024 * 1024, backups=3, max_scopes=32, repeat_seconds=60):
        super().__init__(logging.DEBUG)
        self.root = Path(root)
        self.max_bytes, self.backups = max_bytes, backups
        self.max_scopes, self.repeat_seconds = max_scopes, repeat_seconds
        self.routes = OrderedDict()
        self.repeats = OrderedDict()
        self.activity_format = ActivityFormatter()

    def _route(self, guild):
        if guild not in self.routes:
            while len(self.routes) >= self.max_scopes:
                _, pair = self.routes.popitem(last=False)
                for handler in pair:
                    handler.close()
            pair = []
            for detail in (False, True):
                path = log_path(self.root, guild, detail)
                path.parent.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(path, maxBytes=self.max_bytes, backupCount=self.backups, encoding="utf-8", delay=True)
                handler.setFormatter(DetailFormatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S") if detail else self.activity_format)
                pair.append(handler)
            self.routes[guild] = pair
        self.routes.move_to_end(guild)
        return self.routes[guild]

    def emit(self, record):
        try:
            record = copy.copy(record)
            context_guild, context_channel = log_identity.get()
            guild = getattr(record, "guild_id", context_guild)
            guild = str(int(guild)) if guild is not None else None
            log_path(self.root, guild)  # Validate before creating a directory.
            record.channel_id = getattr(record, "channel_id", context_channel)
            activity, detail = self._route(guild)
            detail.handle(record)
            if record.levelno < logging.INFO:
                return
            key = (guild, record.channel_id, record.name, record.levelno, record.getMessage(), str(record.exc_info[1]) if record.exc_info else "")
            now = time.monotonic()
            last, count = self.repeats.get(key, (float("-inf"), 0))
            if now - last < self.repeat_seconds:
                self.repeats[key] = (last, count + 1)
                self.repeats.move_to_end(key)
                return
            self.repeats[key] = (now, 0)
            self.repeats.move_to_end(key)
            while len(self.repeats) > 2048:
                self.repeats.popitem(last=False)
            if count:
                record.msg = record.getMessage() + f" [repeated {count} more times in the previous interval]"
                record.args = ()
            activity.handle(record)
        except Exception:
            self.handleError(record)

    def close(self):
        self.acquire()
        try:
            for pair in self.routes.values():
                for handler in pair:
                    handler.close()
            self.routes.clear()
            super().close()
        finally:
            self.release()


def configure_logging(root):
    logger = logging.getLogger()
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    logger.setLevel(logging.DEBUG)
    handler = ScopedLogHandler(root)
    logger.addHandler(handler)
    # Handshake, heartbeat and HTTP internals are not useful activity messages.
    for name in ("discord", "aiohttp", "asyncio", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler
