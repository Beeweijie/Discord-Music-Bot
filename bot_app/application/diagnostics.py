"""Task-local log identity; no dependency on a logging backend or Discord."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

log_identity = ContextVar("log_identity", default=(None, None))


@contextmanager
def log_scope(guild_id, channel_id=None):
    token = log_identity.set((guild_id, channel_id))
    try:
        yield
    finally:
        log_identity.reset(token)


def session_logs(method):
    """Scope an async method whose first argument is a session or guild:channel key."""
    @wraps(method)
    async def scoped(self, session, *args, **kwargs):
        if isinstance(session, str) and ":" in session:
            guild, channel = session.split(":", 1)
        else:
            guild, channel = getattr(session, "guild_id", None), getattr(session, "channel_id", None)
        with log_scope(guild, channel):
            return await method(self, session, *args, **kwargs)
    return scoped
