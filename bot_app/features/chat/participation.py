"""Deterministic timing policy; semantic reply/skip choice remains with the model."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ParticipationPlan:
    delay: float
    interval: float
    followup: bool
    activity: int


def participation_plan(state,cfg,now):
    current=state.messages[-1]
    since=max(0,now-state.last_sent)
    active=cfg.get('participation_style')=='active'
    recent=[row for row in list(state.messages)[-15:] if not row.is_bot and current.created_at-row.created_at<20]
    followup=bool(state.last_sent and since<90 and current.author_id==state.last_author)
    question=any(mark in current.content for mark in ('?','？','怎么','什么','为什么','有没有','咋'))
    if current.direct:
        return ParticipationPlan(0.35,0,followup,len(recent))
    if not active:
        return ParticipationPlan(cfg['debounce_seconds'],cfg['cooldown_seconds'],followup,len(recent))
    base=min(8,cfg['cooldown_seconds'])
    interval=min(base,2 if followup else 4 if question else 8)
    if len(recent)>=6 and not followup:
        interval=min(15,interval*1.5)
    delay=min(cfg['debounce_seconds'],0.7 if followup else 1.2 if question else 1.8)
    return ParticipationPlan(delay,interval,followup,len(recent))
