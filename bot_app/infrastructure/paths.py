"""Compatibility import; implementation lives in bot_app.shared.paths."""
from importlib import import_module
import sys
sys.modules[__name__] = import_module("bot_app.shared.paths")
