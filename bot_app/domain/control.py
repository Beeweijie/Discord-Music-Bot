"""Validation shared by every management transport."""
CONTROL_ACTIONS = frozenset({"pause", "resume", "skip", "stop", "clear", "volume", "loop", "seek",
                             "shuffle", "fair", "dedupe", "surprise", "remove", "nextup", "move", "sleep", "replay"})


def validate_control(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Control payload must be an object.")
    action, session_id = value.get("action"), value.get("session_id")
    if not isinstance(action, str) or action not in CONTROL_ACTIONS:
        raise ValueError("Unsupported control action.")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 100:
        raise ValueError("A valid session ID is required.")
    command = {"action": action, "session_id": session_id}
    if action in {"remove", "nextup", "move"}:
        for key in (["index", "to_index"] if action == "move" else ["index"]):
            if type(value.get(key)) is not int or not 1 <= value[key] <= 1000:
                raise ValueError("Queue position must be between 1 and 1000.")
            command[key] = value[key]
        version = value.get("queue_version")
        if not isinstance(version, str) or len(version) != 16:
            raise ValueError("Refresh the queue before trying again.")
        command["queue_version"] = version
    if action == "sleep":
        minutes = value.get("minutes")
        if type(minutes) is not int or not 0 <= minutes <= 1440:
            raise ValueError("Enter 0-1440 minutes; 0 cancels the timer.")
        command["minutes"] = minutes
    if action == "volume":
        volume = value.get("volume")
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError("Volume must be an integer between 0 and 100.")
        command["volume"] = volume
    if action == "seek":
        command["position"] = parse_seek_position(value.get("position"))
    if action == "loop":
        mode = value.get("mode")
        if mode not in ("off", "one", "queue"):
            raise ValueError("Loop mode must be off, one, or queue.")
        command["mode"] = mode
    return command


def parse_seek_position(value):
    """Accept absolute seconds or minutes:seconds, with a bounded FFmpeg offset."""
    if isinstance(value, str):
        parts = value.strip().split(":")
        if not 1 <= len(parts) <= 2 or any(not p.isascii() or not p.isdigit() for p in parts):
            raise ValueError("Enter seconds or m:ss, such as 90 or 1:30.")
        if len(parts) == 2 and int(parts[1]) >= 60:
            raise ValueError("Seconds in m:ss must be between 0 and 59.")
        value = int(parts[0]) if len(parts) == 1 else int(parts[0]) * 60 + int(parts[1])
    if type(value) is not int or not 0 <= value <= 86400:
        raise ValueError("Seek position must be between 0 and 86400 seconds.")
    return value
