"""Welcome templates support only literal, named placeholder replacement."""
import re

DEFAULT_WELCOME_MESSAGE = "欢迎 {user} 加入 **{server}**！🎉"
DEFAULT_MEMBER_COUNT_MESSAGE = "你是第 **{count}** 位成员。"
MAX_WELCOME_LENGTH = 2000


def render_welcome_message(template, *, user, server, count):
    if not template:
        template = DEFAULT_WELCOME_MESSAGE
        if count is not None:
            template += "\n" + DEFAULT_MEMBER_COUNT_MESSAGE
    values = {"user": user, "server": server, "count": str(count) if count is not None else "?"}
    message = re.sub(r"\{(user|server|count)\}", lambda match: values[match[1]], template)
    # Placeholder expansion must still fit Discord's message limit, including emoji.
    return message.encode("utf-16-le")[:MAX_WELCOME_LENGTH * 2].decode("utf-16-le", errors="ignore")
