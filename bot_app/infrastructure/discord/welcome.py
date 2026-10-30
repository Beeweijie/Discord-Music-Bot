"""Compatibility import; implementation lives in bot_app.features.welcome.delivery."""
from importlib import import_module
import sys
sys.modules[__name__] = import_module("bot_app.features.welcome.delivery")
