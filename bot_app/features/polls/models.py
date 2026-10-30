"""Validation for Discord-native polls, independent of transport."""
from urllib.parse import urlsplit


def validate_poll(question, options, hours=24, multiple=False):
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 300:
        raise ValueError("问题必须为 1–300 个字符")
    if not isinstance(options, str):
        raise ValueError("请用 | 分隔选项，例如 苹果|香蕉|橙子")
    answers = [answer.strip() for answer in options.split("|")]
    if not 2 <= len(answers) <= 10:
        raise ValueError("投票需要 2–10 个选项，用 | 分隔")
    if any(not answer or len(answer) > 55 for answer in answers):
        raise ValueError("每个选项必须为 1–55 个字符，不能有空选项")
    if len({answer.casefold() for answer in answers}) != len(answers):
        raise ValueError("投票选项不能重复")
    if type(hours) is not int or not 1 <= hours <= 768:
        raise ValueError("时长必须为 1–768 小时的整数")
    if type(multiple) is not bool:
        raise ValueError("多选参数必须为 true 或 false")
    return {"question": question.strip(), "answers": answers, "hours": hours, "multiple": multiple}


def poll_message_target(value, guild_id, channel_id):
    value = value.strip().strip("<>")
    if value.isascii() and value.isdigit():
        target = (str(guild_id), str(channel_id), value)
    else:
        url = urlsplit(value)
        parts = url.path.strip("/").split("/")
        if url.scheme != "https" or url.hostname not in {"discord.com", "www.discord.com", "canary.discord.com", "ptb.discord.com"} or len(parts) != 4 or parts[0] != "channels":
            raise ValueError("请输入投票消息链接，或本频道中的投票消息 ID")
        target = tuple(parts[1:])
    if not all(x.isascii() and x.isdigit() and 0 < int(x) < 2**64 for x in target):
        raise ValueError("无效的投票消息 ID")
    if int(target[0]) != guild_id:
        raise ValueError("只能操作当前服务器的投票")
    return int(target[1]), int(target[2])
