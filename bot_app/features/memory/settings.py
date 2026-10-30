DEFAULTS = {"enabled": False, "model": "deepseek-flash", "batch_messages": 30,
            "batch_seconds": 300, "retention_days": 30, "recall_limit": 6,
            "semantic_search": False, "daily_model_calls": 60}
EFFECTS = {key: "next_operation" for key in DEFAULTS}

def validate(value):
    for key in ("enabled", "semantic_search"):
        if type(value[key]) is not bool:
            raise ValueError(f"{key} must be boolean.")
    if not isinstance(value["model"], str) or not 1 <= len(value["model"]) <= 100:
        raise ValueError("Invalid memory model.")
    for key, (lo, hi) in {"batch_messages": (1, 100), "batch_seconds": (10, 3600),
                          "retention_days": (1, 365), "recall_limit": (1, 12), "daily_model_calls": (1, 1000)}.items():
        if type(value[key]) is not int or not lo <= value[key] <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}.")
    return dict(value)
