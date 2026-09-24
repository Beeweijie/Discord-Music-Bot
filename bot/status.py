"""Atomic runtime status and local tray commands."""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PROJECT_ROOT / "runtime"
STATUS_FILE = RUNTIME_DIR / "status.json"
CONTROL_FILE = RUNTIME_DIR / "control.jsonl"  # Legacy; never replay old requests.
COMMANDS_DIR = RUNTIME_DIR / "commands"
CONTROL_MAX_AGE_SECONDS = 60
_status_lock = threading.RLock()
logger = logging.getLogger(__name__)


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_status() -> dict:
    try:
        value = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def read_status() -> dict:
    with _status_lock:
        return _read_status()


def write_status(*, reset: bool = False, **updates: Any) -> bool:
    """Merge an update. A locked or unavailable status file cannot stop music."""
    with _status_lock:
        try:
            status = {} if reset else _read_status()
            status.update(updates)
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_json(STATUS_FILE, status)
            return True
        except (OSError, TypeError, ValueError):
            logger.warning("Could not update runtime status", exc_info=True)
            return False


def write_music_session(session_id: str, data: dict[str, Any]) -> bool:
    with _status_lock:
        sessions = _read_status().get("music_sessions")
        sessions = sessions if isinstance(sessions, dict) else {}
        sessions[session_id] = data
        return write_status(music_sessions=sessions)


def remove_music_session(session_id: str) -> bool:
    with _status_lock:
        sessions = _read_status().get("music_sessions")
        sessions = sessions if isinstance(sessions, dict) else {}
        sessions.pop(session_id, None)
        return write_status(music_sessions=sessions)


def enqueue_control(command: dict) -> str:
    """Publish one complete command; let the caller report write failures."""
    command_id = uuid.uuid4().hex
    payload = dict(command)
    payload.update(id=command_id, created_at=datetime.now(timezone.utc).isoformat())
    payload.setdefault("instance_id", read_status().get("instance_id"))
    _atomic_json(COMMANDS_DIR / f"{command_id}.json", payload)
    return command_id


def write_control_result(command: dict, ok: bool, message: str) -> bool:
    return write_status(last_control={
        "id": command.get("id"), "action": command.get("action"),
        "session_id": command.get("session_id"), "ok": bool(ok),
        "message": str(message), "completed_at": datetime.now(timezone.utc).isoformat(),
    })


def drain_control_commands() -> list[dict]:
    """Claim up to 100 requests, rejecting malformed, stale or prior-run data."""
    commands = []
    try:
        paths = sorted(COMMANDS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)[:100]
    except OSError:
        logger.warning("Could not read tray commands", exc_info=True)
        return commands
    now = datetime.now(timezone.utc)
    instance_id = read_status().get("instance_id")
    for path in paths:
        claimed = path.with_suffix(".processing")
        command = {"id": path.stem}
        try:
            path.rename(claimed)
        except OSError:
            continue
        try:
            payload = json.loads(claimed.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("控制命令格式无效。")
            command = payload
            created = datetime.fromisoformat(command["created_at"])
            if created.tzinfo is None:
                raise ValueError("控制命令缺少时区。")
            age = (now - created).total_seconds()
            if age < -5 or age > CONTROL_MAX_AGE_SECONDS:
                raise ValueError("控制命令已过期，请重试。")
            if command.get("instance_id") != instance_id:
                raise ValueError("机器人已重新启动，请刷新后重试。")
            if not isinstance(command.get("action"), str) or not isinstance(command.get("session_id"), str):
                raise ValueError("控制命令缺少操作或会话。")
            commands.append(command)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            write_control_result(command, False, str(exc))
        finally:
            try:
                claimed.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove consumed command %s", claimed.name)
    return commands
