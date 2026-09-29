"""Compatibility import; implementation lives in bot_app.infrastructure.persistence.runtime."""
import sys
from bot_app.infrastructure.persistence import runtime as _implementation
sys.modules[__name__] = _implementation
