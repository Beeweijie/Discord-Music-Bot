"""Ephemeral social context, grounded only in this channel's admitted recent turns."""
import re
from dataclasses import dataclass

KINDS={'chat','joke','repair','feedback','help','end'}
TTL=15*60


@dataclass
class InteractionState:
    member_id: str = ''
    kind: str = 'chat'
    source_id: str = ''
    reply_id: str = ''
    at: float = 0


def local_kind(text):
    """Direct mentions bypass the judge; use only explicit signals in that case."""
    if re.search(r'别(?:开玩笑|损[我人了]|闹)|没开玩笑|说正经的|认真(?:说|问|的|点)|真的很难受|seriously|stop joking',text,re.I):
        return 'help'
    if re.search(r'不是这个意思|理解错|说反了|搞反了|我(?:说|问)的是|你误会|不是在说你',text):
        return 'repair'
    if re.search(r'(?:你|回复|说话).{0,12}人机|人机味|像客服|客服腔|说话.{0,8}(生硬|奇怪|别扭)|'
                 r'回复.{0,8}(生硬|奇怪|别扭)',text) or text.strip()=='人机':
        return 'feedback'
    if re.fullmatch(r'(?:那我)?(?:先下了|不聊了|晚安|拜拜|再见|先忙了)[。！!～~\s]*',text.strip()):
        return 'end'
    if re.search(r'哈哈|笑死|开玩笑的|逗你的|闹着玩',text):
        return 'joke'
    return 'chat'


def interaction_context(state,cfg,current,decision):
    rows=[row for row in state.messages if row.scope==current.scope
          and row.author_id not in cfg['excluded_users'] and 0<=current.created_at-row.created_at<=TTL]
    by_id={row.message_id:row for row in rows}
    explicit=local_kind(current.content)
    kind=explicit if explicit!='chat' else decision.get('kind','chat')
    if kind not in KINDS:
        kind='chat'
    context={'kind':kind,'member_id':current.author_id}
    previous=state.interaction
    if (previous.member_id==current.author_id and previous.source_id in by_id and previous.reply_id in by_id
            and 0<=current.created_at-previous.at<=TTL):
        context['previous_exchange']={'kind':previous.kind,
            'member':snippet(by_id[previous.source_id]),'reply':snippet(by_id[previous.reply_id])}
    else:
        # Recover a factual anchor from retained history after a restart, without
        # inferring a relationship or importing another member's exchange.
        for index in range(len(rows)-2,0,-1):
            if rows[index].is_bot and not rows[index-1].is_bot and rows[index-1].author_id==current.author_id:
                context['previous_exchange']={'kind':'unknown','member':snippet(rows[index-1]),'reply':snippet(rows[index])}
                break
    pending=[]
    for row in reversed(rows):
        if row.is_bot:
            break
        if row.author_id==current.author_id:
            pending.append(snippet(row))
        if len(pending)>=3:
            break
    context['pending_inputs']=list(reversed(pending))
    if pending:
        previous=context.get('previous_exchange',{}).get('member')
        context['topic_anchor']=previous if previous and len(current.content)<16 and kind=='chat' else pending[-1]
        question=next((row for row in reversed(pending) if re.search(r'[?？]|为什么|怎么|啥|什么|有没有',row['text'])),None)
        if question:
            context['pending_question']=question
    if kind=='repair':
        context['repair_request']=snippet(current)
    # Classification is a tentative interpretation, not proof of a user's intent.
    return context


def snippet(row):
    return {'source_id':row.message_id,'text':row.content[:160]}


def remember_exchange(state,current,decision,sent):
    if sent:
        explicit=local_kind(current.content)
        kind=explicit if explicit!='chat' else decision.get('kind','chat')
        state.interaction=InteractionState(current.author_id,kind if kind in KINDS else 'chat',
            current.message_id,sent[-1].message_id,current.created_at)
