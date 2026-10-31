"""Deterministic timing policy; semantic reply/skip choice remains with the model."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ParticipationPlan:
    delay: float
    interval: float
    followup: bool
    activity: int
    max_wait: float = 0


def incomplete_input(text):
    """Only clear unfinished constructions; lack of punctuation is normal in chat."""
    text=text.strip()
    if not text or text.endswith(('?', '？', '!', '！')):
        return False
    return bool(re.search(r'(?:我跟你[说讲]|等一下|稍等|先别回|等我一下)[，,。\s]*$|'
                          r'(?:但是|因为|还有就是|我想说的是|我的意思是|比如|例如|然后|还有)[，,：:\s]*$|'
                          r'[:：，,]$|\b(?:but|because|for example|wait a moment)[,:\s]*$',text,re.I))


def pending_due(state,cfg,now):
    """Bound an adaptive quiet window so a fast typist cannot postpone us forever."""
    plan=participation_plan(state,cfg,now)
    limit=max(cfg['max_wait_seconds'],plan.delay,plan.max_wait)
    due=min(state.first_pending+limit,state.last_pending+plan.delay)
    if cfg['participation_style']=='active' and not state.messages[-1].direct:
        due=max(due,state.last_sent+plan.interval)
    return due


def response_nudge(state,cfg):
    """An explicit ping in our own uninterrupted exchange needs no semantic gate."""
    if cfg.get('participation_style')!='active' or not state.messages:
        return False
    current=state.messages[-1]
    if current.is_bot or (current.mentioned_user_ids and current.bot_user_id not in current.mentioned_user_ids):
        return False
    if not re.fullmatch(r'(?:[?？]+|回(?:我)?(?:一下|个话|话|一声)?|说(?:句话|话)|还在(?:吗|么)?|在吗|'
                        r'怎么说[，,\s]*(?:现在呢|呢)|怎么不(?:回|说话))[?？!！。\s]*',current.content.strip()):
        return False
    if state.last_author!=current.author_id:
        return False
    max_age=1800 if re.fullmatch(r'[?？]+',current.content.strip()) else 3600
    for row in reversed(list(state.messages)[:-1]):
        if row.is_bot:
            return 0<=current.created_at-row.created_at<max_age
        if row.author_id!=current.author_id:
            return False
    return False


def participation_plan(state,cfg,now):
    current=state.messages[-1]
    since=max(0,now-state.last_sent)
    active=cfg.get('participation_style')=='active'
    recent=[row for row in list(state.messages)[-15:] if not row.is_bot and current.created_at-row.created_at<20]
    same_exchange=True
    for row in reversed(state.messages):
        if row.is_bot:
            break
        if row.author_id!=current.author_id:
            same_exchange=False
            break
    followup=bool(state.last_sent and since<(900 if active else 90)
                  and current.author_id==state.last_author and same_exchange)
    question=any(mark in current.content for mark in ('?','？','怎么','什么','为什么','有没有','咋'))
    # Consecutive fragments from the same person deserve a little room to finish.
    burst=1
    for row in reversed(list(state.messages)[:-1]):
        if row.is_bot or row.author_id!=current.author_id or not 0<=current.created_at-row.created_at<=8:
            break
        burst+=1
    unfinished=incomplete_input(current.content)
    if current.direct:
        return ParticipationPlan(6 if unfinished else 3 if burst>1 else .8,0,followup,len(recent),
                                 8 if unfinished or burst>1 else 0)
    if not active:
        return ParticipationPlan(cfg['debounce_seconds'],cfg['cooldown_seconds'],followup,len(recent))
    base=min(8,cfg['cooldown_seconds'])
    interval=min(base,2 if followup else 4 if question else 8)
    if len(recent)>=6 and not followup:
        interval=min(15,interval*1.5)
    delay=(7 if unfinished else 4.5 if burst>1 else 2.2 if followup else 2.5 if question else 3)
    delay=0 if cfg['debounce_seconds']==0 else max(min(cfg['debounce_seconds'],8),delay)
    return ParticipationPlan(delay,interval,followup,len(recent),8 if unfinished or burst>1 else 0)
