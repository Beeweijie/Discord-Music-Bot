"""Open the tray UI, or start the tray app if it is not running."""

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts.ui_protocol import notify_existing_ui


def find_python() -> str:
    candidates = [
        PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe",
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / "venv" / "Scripts" / "pythonw.exe",
        PROJECT_ROOT / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def main():
    if notify_existing_ui():
        return

    python_exe = find_python()
    subprocess.Popen(
        [python_exe, str(PROJECT_ROOT / "scripts" / "tray_app.py"), "--show"],
        cwd=PROJECT_ROOT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


if __name__ == "__main__":
    main()
