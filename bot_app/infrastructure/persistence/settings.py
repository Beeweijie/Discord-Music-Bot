"""Compatibility import; implementation lives in bot_app.app.configuration."""
from importlib import import_module
import sys
sys.modules[__name__] = import_module("bot_app.app.configuration")
