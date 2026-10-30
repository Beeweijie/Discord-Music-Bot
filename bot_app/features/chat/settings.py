from bot_app.shared.identity import snowflake

DEFAULTS = {
    "enabled": False, "channels": [], "channel_overrides": {}, "mode": "observe",
    "name": "喵酱", "aliases": ["喵酱"],
    "persona": "你是群里的AI成员喵酱。说话自然简短，通常一到三句话。区分聊天对象，少打断别人，不重复、不装懂，不编造现实身份和个人经历。",
    "interests": "音乐、游戏、日常闲聊", "model": "deepseek-flash",
    "debounce_seconds": 5, "max_wait_seconds": 15, "cooldown_seconds": 90,
    "hourly_limit": 10, "daily_limit": 60, "daily_model_calls": 300,
    "context_messages": 50, "context_chars": 16000, "max_reply_chars": 600,
    "quiet_hours": "", "timezone": "Australia/Sydney", "excluded_users": [],
}
EFFECTS = {key: "next_message" for key in DEFAULTS}

def validate(value):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    import re
    result = dict(value)
    if type(value["enabled"]) is not bool or value["mode"] not in {"observe", "participate"}:
        raise ValueError("Invalid chat switch or mode.")
    bounds = {"debounce_seconds": (0, 60), "max_wait_seconds": (1, 120), "cooldown_seconds": (0, 3600),
              "hourly_limit": (1, 100), "daily_limit": (1, 1000), "daily_model_calls": (1, 5000),
              "context_messages": (5, 100), "context_chars": (2000, 40000), "max_reply_chars": (50, 1500)}
    for key, (lo, hi) in bounds.items():
        if type(value[key]) is not int or not lo <= value[key] <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}.")
    if value["max_wait_seconds"] < value["debounce_seconds"]:
        raise ValueError("Maximum wait must be at least the debounce interval.")
    for key in ("name", "persona", "interests", "model", "timezone", "quiet_hours"):
        if not isinstance(value[key], str) or len(value[key]) > (6000 if key == "persona" else 500):
            raise ValueError(f"Invalid {key}.")
    if not value["name"].strip() or not value["model"].strip():
        raise ValueError("Name and model are required.")
    for key in ("channels", "excluded_users"):
        if not isinstance(value[key], list) or len(value[key]) > 200:
            raise ValueError(f"Invalid {key}.")
        result[key] = list(dict.fromkeys(snowflake(item) for item in value[key]))
    if not isinstance(value["aliases"], list) or len(value["aliases"]) > 20 or any(not isinstance(x, str) or not x.strip() or len(x) > 40 for x in value["aliases"]):
        raise ValueError("Aliases must contain up to 20 short names.")
    if not isinstance(value["channel_overrides"], dict) or len(value["channel_overrides"]) > 200:
        raise ValueError("Invalid channel overrides.")
    allowed = {"mode", "persona", "interests", "cooldown_seconds", "hourly_limit", "daily_limit"}
    for channel, overrides in value["channel_overrides"].items():
        snowflake(channel)
        if not isinstance(overrides, dict) or not set(overrides) <= allowed:
            raise ValueError("Unsupported channel override.")
        merged = dict(value, **overrides, channel_overrides={})
        validate(merged)
    try:
        ZoneInfo(value["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown timezone; install tzdata on Windows if unavailable.") from None
    if value["quiet_hours"] and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", value["quiet_hours"]):
        raise ValueError("Quiet hours use HH:MM-HH:MM.")
    return result

def for_channel(value, channel_id):
    return dict(value, **value["channel_overrides"].get(str(channel_id), {}))
