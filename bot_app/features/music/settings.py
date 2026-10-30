"""Music configuration and validation, owned by the music feature."""
from urllib.parse import urlsplit

DEFAULT_PLAYLIST_URL = "https://music.youtube.com/playlist?list=PLH6zD0MCw2r4"
MUSIC_DEFAULTS = {"default_volume": 40, "max_queue_size": 1000, "max_playlist_songs": 1000,
                  "default_playlist_url": DEFAULT_PLAYLIST_URL, "predownload_count": 3,
                  "idle_timeout_seconds": 300, "cache_max_age_seconds": 3600}
EFFECTS = {key: "next_operation" for key in MUSIC_DEFAULTS}
EFFECTS.update(default_volume="new_session", idle_timeout_seconds="next_idle_check", cache_max_age_seconds="next_cache_cleanup")

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

def validate(value):
    bounds = {"default_volume": (0, 100), "max_queue_size": (1, 1000), "max_playlist_songs": (1, 1000),
              "predownload_count": (0, 10), "idle_timeout_seconds": (30, 86400), "cache_max_age_seconds": (60, 604800)}
    for key, (low, high) in bounds.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}.")
    validate_playlist_url(value["default_playlist_url"])
    return dict(value)
