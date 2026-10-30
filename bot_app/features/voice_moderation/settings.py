VOICE_DEFAULTS = {"enabled": False, "level": "normal", "model": "small"}
EFFECTS = {key: "stored_only" for key in VOICE_DEFAULTS}

def validate(value):
    if type(value["enabled"]) is not bool:
        raise ValueError("enabled must be a boolean value.")
    if value["level"] not in ("low", "normal", "strict"):
        raise ValueError("Invalid moderation level.")
    if value["model"] not in ("small", "medium"):
        raise ValueError("Invalid voice model.")
    return dict(value)
