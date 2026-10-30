"""Immutable platform identities; never populated from model output."""
from dataclasses import dataclass

def snowflake(value):
    value = str(value)
    if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**64:
        raise ValueError("Invalid Discord ID.")
    return str(int(value))

@dataclass(frozen=True)
class Scope:
    guild_id: str
    channel_id: str

    def __post_init__(self):
        object.__setattr__(self, "guild_id", snowflake(self.guild_id))
        object.__setattr__(self, "channel_id", snowflake(self.channel_id))

    @property
    def key(self):
        return f"discord:{self.guild_id}:{self.channel_id}"

@dataclass(frozen=True)
class MessageEvent:
    scope: Scope
    message_id: str
    author_id: str
    author_name: str
    content: str
    created_at: float
    reply_to: str = ""
    direct: bool = False
    is_bot: bool = False

    def __post_init__(self):
        object.__setattr__(self, "message_id", snowflake(self.message_id))
        object.__setattr__(self, "author_id", snowflake(self.author_id))

    def context(self):
        return {"message_id": self.message_id, "author_id": self.author_id,
                "name": self.author_name[:100], "text": self.content[:4000],
                "timestamp": self.created_at, "reply_to": self.reply_to,
                "role": "assistant" if self.is_bot else "member"}
