"""Conservative, bounded routing for optional deeper reasoning."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ThinkingPlan:
    effort: str = 'none'
    max_tokens: int = 300
    reason: str = 'ordinary_chat'


def thinking_plan(text,cfg,decision=None):
    if cfg.get('thinking_mode','off')!='auto':
        return ThinkingPlan(max_tokens=cfg.get('reply_max_tokens',300),reason='disabled')
    if re.search(r'深度思考|仔细(?:思考|分析|想)|认真(?:分析|推理)|think (?:deeply|carefully)|step.by.step',text,re.I):
        return ThinkingPlan('high',2048,'explicit_request')
    if re.search(r'证明|推导|算法|排查|诊断|复杂规划|分析.{0,12}(代码|原因)|对比.{0,12}(方案|优缺点)|debug|prove|derive',text,re.I):
        return ThinkingPlan('low',1536,'complex_task')
    if len(text)>=20 and (decision or {}).get('thinking') in {'low','high'}:
        return ThinkingPlan('low',1536,'semantic_complexity')
    return ThinkingPlan(max_tokens=cfg.get('reply_max_tokens',300))
