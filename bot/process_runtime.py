"""Compatibility import; implementation lives in bot_app.shared.process_runtime."""
import sys
from bot_app.infrastructure.processes import runtime as _implementation
sys.modules[__name__] = _implementation
