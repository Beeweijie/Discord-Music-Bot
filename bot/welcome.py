"""Compatibility import for welcome extensions and callers."""
import sys
from bot_app.features.welcome import discord as _implementation
sys.modules[__name__] = _implementation
