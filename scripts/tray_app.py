"""Compatibility launcher for the desktop frontend."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot_app.presentation.desktop.tray import TrayApp, main

if __name__ == "__main__":
    main()
