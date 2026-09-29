"""Compatibility import for the desktop wake-up protocol."""
import sys
from bot_app.infrastructure.processes import ui_protocol as _implementation
sys.modules[__name__] = _implementation
