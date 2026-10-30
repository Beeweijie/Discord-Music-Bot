"""Compatibility import; implementation lives in bot_app.shared.runtime."""
import sys
from bot_app.shared import runtime as _implementation
sys.modules[__name__] = _implementation
