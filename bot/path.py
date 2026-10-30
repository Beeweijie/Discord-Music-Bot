"""Compatibility import; implementation lives in bot_app.shared.paths."""
import sys
from bot_app.infrastructure import paths as _implementation
sys.modules[__name__] = _implementation
