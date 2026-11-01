"""Welcome configuration and validation."""
from .models import MAX_WELCOME_LENGTH

WELCOME_DEFAULTS = {"enabled": True, "include_bots": True, "channel_id": "0", "message": ""}
EFFECTS = {key: "next_event" for key in WELCOME_DEFAULTS}
EFFECTS["enabled"] = "next_event_or_restart"

def validate(value):
    if type(value["enabled"]) is not bool or type(value["include_bots"]) is not bool:
        raise ValueError("Welcome switches must be boolean values.")
    channel_id = value["channel_id"]
    if not isinstance(channel_id, str) or not channel_id.isascii() or not channel_id.isdigit() or not 0 <= int(channel_id) < 2**64:
        raise ValueError("Channel ID must be numeric; 0 uses the server system channel.")
    message = value["message"]
    if not isinstance(message, str) or len(message.encode("utf-16-le")) > MAX_WELCOME_LENGTH * 2:
        raise ValueError(f"Welcome message must be text up to {MAX_WELCOME_LENGTH} characters (emoji may count as two).")
    return dict(value, channel_id=str(int(channel_id)), message=message.strip())
