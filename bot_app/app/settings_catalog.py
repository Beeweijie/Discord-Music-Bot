"""Explicit composition of feature-owned configuration schemas."""
from bot_app.features.music import settings as music
from bot_app.features.welcome import settings as welcome
from bot_app.features.voice_moderation import settings as moderation
from bot_app.features.chat import settings as chat
from bot_app.features.memory import settings as memory
from bot_app.shared.revisions import revision

MUSIC_DEFAULTS = music.MUSIC_DEFAULTS
WELCOME_DEFAULTS = welcome.WELCOME_DEFAULTS
VOICE_DEFAULTS = moderation.VOICE_DEFAULTS
DEFAULT_PLAYLIST_URL = music.DEFAULT_PLAYLIST_URL
validate_playlist_url = music.validate_playlist_url
SCHEMAS = {"music": (MUSIC_DEFAULTS, music.validate, music.EFFECTS),
           "welcome": (WELCOME_DEFAULTS, welcome.validate, welcome.EFFECTS),
           "voice_moderation": (VOICE_DEFAULTS, moderation.validate, moderation.EFFECTS),
           "chat": (chat.DEFAULTS, chat.validate, chat.EFFECTS),
           "memory": (memory.DEFAULTS, memory.validate, memory.EFFECTS)}
DEFAULTS = {key: item[0] for key, item in SCHEMAS.items()}
EFFECTS = {key: item[2] for key, item in SCHEMAS.items()}

def validate_settings(section, value):
    if section not in SCHEMAS:
        raise ValueError("Unsupported settings section.")
    if not isinstance(value, dict) or set(value) != set(DEFAULTS[section]):
        raise ValueError("Missing or unknown settings fields.")
    return SCHEMAS[section][1](value)
