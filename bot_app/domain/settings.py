"""Persisted settings, validation and explicit application semantics."""
import hashlib
import json
from urllib.parse import urlsplit

DEFAULT_PLAYLIST_URL = "https://music.youtube.com/playlist?list=PLH6zD0MCw2r4"

VOICE_DEFAULTS = {"enabled": False, "level": "normal", "model": "small"}
MUSIC_DEFAULTS = {"default_volume": 40, "max_queue_size": 500, "max_playlist_songs": 500,
                  "default_playlist_url": DEFAULT_PLAYLIST_URL, "predownload_count": 3, "idle_timeout_seconds": 300, "cache_max_age_seconds": 3600}
WELCOME_DEFAULTS = {"enabled": True, "include_bots": True, "channel_id": "0"}
DEFAULTS = {"music": MUSIC_DEFAULTS, "welcome": WELCOME_DEFAULTS, "voice_moderation": VOICE_DEFAULTS}
EFFECTS = {
    "music": {"default_playlist_url": "next_operation", "default_volume": "new_session", "max_queue_size": "next_operation",
              "max_playlist_songs": "next_operation", "predownload_count": "next_operation",
              "idle_timeout_seconds": "next_idle_check", "cache_max_age_seconds": "next_cache_cleanup"},
    "welcome": {"enabled": "next_event_or_restart", "include_bots": "next_event", "channel_id": "next_event"},
    "voice_moderation": {key: "stored_only" for key in VOICE_DEFAULTS},
}


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def validate_settings(section: str, value: dict) -> dict:
    if section not in DEFAULTS:
        raise ValueError("Unsupported settings section.")
    if not isinstance(value, dict) or set(value) != set(DEFAULTS[section]):
        raise ValueError("Missing or unknown settings fields.")
    if section == "music":
        bounds = {"default_volume": (0, 100), "max_queue_size": (1, 1000),
                  "max_playlist_songs": (1, 1000), "predownload_count": (0, 10),
                  "idle_timeout_seconds": (30, 86400), "cache_max_age_seconds": (60, 604800)}
        for key, (low, high) in bounds.items():
            if type(value[key]) is not int or not low <= value[key] <= high:
                raise ValueError(f"{key} must be an integer between {low} and {high}.")
        validate_playlist_url(value["default_playlist_url"])
        return dict(value)
    if section == "welcome":
        if type(value["enabled"]) is not bool or type(value["include_bots"]) is not bool:
            raise ValueError("Welcome switches must be boolean values.")
        channel_id = value["channel_id"]
        if not isinstance(channel_id, str) or not channel_id.isascii() or not channel_id.isdigit() or not 0 <= int(channel_id) < 2**64:
            raise ValueError("Channel ID must be numeric; 0 uses the server system channel.")
        return dict(value, channel_id=str(int(channel_id)))
    if type(value["enabled"]) is not bool:
        raise ValueError("enabled must be a boolean value.")
    if value["level"] not in ("low", "normal", "strict"):
        raise ValueError("Invalid moderation level.")
    if value["model"] not in ("small", "medium"):
        raise ValueError("Invalid voice model.")
    return dict(value)


def validate_playlist_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError("Enter a valid playlist URL.")
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        allowed = any(host == d or host.endswith("." + d) for d in ("youtube.com", "youtu.be", "bilibili.com", "b23.tv"))
        if url.scheme not in ("http", "https") or not allowed or url.username or url.password:
            raise ValueError()
    except ValueError:
        raise ValueError("Default playlist must be a YouTube or Bilibili HTTP(S) URL.") from None
    return value
