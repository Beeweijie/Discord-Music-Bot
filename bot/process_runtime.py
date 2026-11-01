"""Compatibility import; implementation lives in bot_app.shared.process_runtime."""
import sys
from bot_app.shared import process_runtime as _implementation
sys.modules[__name__] = _implementation
