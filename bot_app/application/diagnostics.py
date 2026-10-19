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
    """Scope an async method whose first argument is a player or server key."""
    @wraps(method)
    async def scoped(self, session, *args, **kwargs):
        if isinstance(session, str):
            player = getattr(self, "sessions", {}).get(session)
            guild = getattr(player, "guild_id", session.split(":", 1)[0])
            channel = getattr(player, "channel_id", None)
        else:
            guild, channel = getattr(session, "guild_id", None), getattr(session, "channel_id", None)
        with log_scope(guild, channel):
            return await method(self, session, *args, **kwargs)
    return scoped
