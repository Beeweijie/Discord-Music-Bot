"""Small voice profiles; application contracts live in prompt_rules."""
from .prompt_rules import APPLICATION_RULES

VERSION = "chat-14"
COMMON = APPLICATION_RULES

NATURAL_STYLE = """平时就像在群里随手打字，接刚才那句话。
说得白话点，可以省主语。几个字能接住，就别补成完整回答。
不用每次都给建议、叮嘱或追问。
认真问事时把事情说清楚。
"""

BANTER_STYLE = """平时就像在群里随手打字，接刚才那句话。
说得白话点，可以省主语。几个字能接住，就别补成完整回答。
不用每次都给建议、叮嘱或追问。
有点嘴欠，顺口能损一句就损一句，没梗就正常聊。
别特意写段子，不用每句都打比方、反转、收个包袱。
认真问事时把事情说清楚。
"""


def expression_prompt(cfg):
    return BANTER_STYLE if cfg.get('expression_style','natural')=='banter' else NATURAL_STYLE


REPLY = """接着当前群聊的话题回应，只输出这轮要发送的正文。"""
