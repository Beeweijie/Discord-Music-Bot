"""Compatibility import for welcome extensions and callers."""
import sys
from bot_app.presentation.discord import welcome as _implementation
sys.modules[__name__] = _implementation
