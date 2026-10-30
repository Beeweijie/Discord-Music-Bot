"""Channel-owned conversation scheduling; no Discord or storage SDK imports."""
import asyncio
import functools
import hashlib
import json
import logging
import random
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from bot_app.shared.revisions import revision
from bot_app.shared.redaction import contains_secret
from bot_app.shared.llm.deepseek import BudgetExceeded
from .settings import for_channel
from .prompts import COMMON, expression_prompt, VERSION
from .participation import participation_plan, response_nudge
from .reply import ReplyGenerator
from .thinking import thinking_plan, ThinkingPlan
from .judgement import judge_messages, normalize, needs_deep_memory, estimated_tokens, DecisionError
from .delivery import reply_parts

logger = logging.getLogger(__name__)

@dataclass
class ChannelState:
    messages: deque = field(default_factory=lambda: deque(maxlen=100))
    sequence: int = 0
    processed: int = 0
    first_pending: float = 0
    last_pending: float = 0
    last_sent: float = 0
    last_author: str = ''
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: object = None
    send: object = None
    can_send: object = None
    context_anchor: str = ''

class ChatService:
    def __init__(self, repository, model, memory, settings_for, web=None, persona=None, vision=None):
        self.repository = repository
        self.model = model
        self.memory = memory
        self.settings_for = settings_for
        self.persona, self.vision = persona, vision
        self.replies = ReplyGenerator(model,web,on_web=persona.observe_web if persona else None)
        self.channels = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='chat-db')
        self.closing = False
        self.last_error = None
        self.user_rates = {}
        self.on_human = None
        self.pause = asyncio.sleep

    async def call(self, method, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(self.executor, functools.partial(getattr(self.repository, method), *args, **kwargs))

    def config(self, scope):
        _, guild, channel = scope.split(':')
        return for_channel(self.settings_for(guild), channel)

    def allowed(self, scope):
        cfg = self.config(scope)
        return cfg['enabled'] and scope.split(':')[2] in cfg['channels']

    async def start(self):
        if self.repository.path.exists():
            await self.call('retire')

    @staticmethod
    def quiet(cfg):
        if not cfg['quiet_hours']:
            return False
        now = datetime.now(ZoneInfo(cfg['timezone'])).strftime('%H:%M')
        start, end = cfg['quiet_hours'].split('-')
        return start <= now < end if start < end else now >= start or now < end

    async def receive(self, event, send, can_send):
        scope = event.scope.key
        if self.closing or not self.allowed(scope):
            return
        cfg = self.config(scope)
        if event.author_id in cfg['excluded_users']:
            return
        if contains_secret(event.content):
            return
        state = self.channels.get(scope)
        if state is None:
            state = ChannelState()
            self.channels[scope] = state
            if self.memory.has_store and self.memory.config(scope)['enabled']:
                try:
                    recent = await self.memory.recent_context(event.scope, cfg['context_messages'])
                except Exception:
                    recent = []
                    logger.warning('Retained context unavailable; starting with current messages')
                previous_author = ''
                for row in recent:
                    if row.author_id not in cfg['excluded_users']:
                        state.messages.append(row)
                        if row.is_bot:
                            state.last_sent = time.monotonic() - max(0, time.time() - row.created_at)
                            state.last_author = previous_author
                        else:
                            previous_author = row.author_id
        if any(message.message_id == event.message_id for message in state.messages):
            return
        state.messages.append(event)
        state.sequence += 1
        state.send, state.can_send = send, can_send
        try:
            await self.memory.ingest(event, cfg['mode'] == 'observe')
        except Exception:
            logger.exception('Memory ingestion unavailable; continuing short-term chat')
        if event.is_bot:
            state.processed = state.sequence
            return
        if self.persona:
            await self.persona.observe(event)
        if self.on_human:
            await self.on_human(event)
        now = time.monotonic()
        if state.first_pending == 0:
            state.first_pending = now
        state.last_pending = now
        state.wake.set()
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(self._drain(scope, state), name='chat.channel')

    async def _drain(self, scope, state):
        try:
            while not self.closing and state.processed < state.sequence and self.allowed(scope):
                cfg = self.config(scope)
                plan=participation_plan(state,cfg,time.monotonic())
                due = min(state.first_pending + cfg['max_wait_seconds'], state.last_pending + plan.delay)
                if cfg['participation_style']=='active' and not state.messages[-1].direct:
                    due=max(due,state.last_sent+plan.interval)
                delay = max(0, due - time.monotonic())
                state.wake.clear()
                if delay:
                    try:
                        await asyncio.wait_for(state.wake.wait(), delay)
                        continue
                    except TimeoutError:
                        pass
                snapshot = state.sequence
                state.first_pending = 0
                try:
                    await self.round(scope, state, snapshot)
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except BudgetExceeded:
                    await self.call('record',scope,'skip','daily_cny_budget')
                except Exception as error:
                    self.last_error = type(error).__name__
                    await self.call('record', scope, 'error', self.last_error)
                    logger.warning('Chat round failed: %s', self.last_error)
                state.processed = snapshot
                if state.sequence > snapshot and state.first_pending == 0:
                    state.first_pending = state.last_pending
        finally:
            state.task = None

    async def round(self, scope, state, snapshot):
        cfg = self.config(scope)
        if not self.allowed(scope) or not state.messages:
            return
        cfg_revision = revision(cfg)
        current = state.messages[-1]
        direct = current.direct
        now = time.monotonic()
        plan=participation_plan(state,cfg,now)
        if not direct and current.mention_only:
            await self.call('record', scope, 'skip', 'addressed_to_other')
            return
        if direct:
            key = (scope, current.author_id)
            if len(self.user_rates) > 2048:
                self.user_rates = {key: rate for key, rate in self.user_rates.items() if rate and rate[-1] > now - 30}
            rate = self.user_rates.setdefault(key, deque(maxlen=10))
            while rate and rate[0] < now - 30:
                rate.popleft()
            if len(rate) >= (8 if cfg['participation_style']=='active' else 3):
                await self.call('record', scope, 'skip', 'member_rate_limit')
                return
            rate.append(now)
        elif self.quiet(cfg) or now - state.last_sent < plan.interval:
            await self.call('record', scope, 'skip', 'quiet_or_cooldown')
            return
        elif not await self.call('allowed', scope, cfg['hourly_limit'], cfg['daily_limit']):
            await self.call('record', scope, 'skip', 'channel_budget')
            return
        memories = []
        memory_revision = revision(self.memory.config(scope))
        shallow=[]
        try:
            shallow=await self.memory.shallow(scope)
        except Exception:
            logger.warning('Shallow memory unavailable')
        target_ids={current.message_id}
        nudged=response_nudge(state,cfg)
        decision_source='direct' if direct else 'nudge' if nudged else 'judge'
        judge_input=0
        if direct or nudged:
            decision = {"action": "reply", "reason": "addressed", "target_id": current.message_id}
        else:
            thin,targets=judge_messages(state,cfg,shallow,plan)
            judge_input=estimated_tokens(''.join(row['content'] for row in thin))
            for attempt in range(2):
                raw=await self.model.complete('chat_judge',thin,scope=scope,model=cfg['model'],
                    json_mode=True,max_tokens=64 if attempt==0 else 96,daily_limit=cfg['daily_model_calls'])
                try:
                    decision=normalize(raw,targets)
                    break
                except DecisionError as error:
                    await self.call('record',scope,'error','judge_'+error.code,
                                    {'prompt':VERSION,'visible_indexes':list(targets),'attempt':attempt+1})
                    logger.warning('Chat judgement rejected: %s',error.code)
                    if attempt or state.sequence!=snapshot or not self.allowed(scope) or revision(self.config(scope))!=cfg_revision:
                        return
                    # One bounded repair; never echo a malformed model response or guess a target.
                    thin=thin+[{'role':'user','content':'Return the specified decision JSON only. '
                        'Use a=r/w/s and an integer i from '+json.dumps(list(targets))+'.'}]
            target_ids=set(targets.values())
        action, reason = decision.get('action'), decision.get('reason')
        valid_reasons = {'addressed', 'followup', 'useful', 'interrupting', 'repetitive', 'unrelated', 'wait'}
        if action not in {'reply', 'wait', 'skip'} or reason not in valid_reasons:
            raise ValueError('Invalid participation decision.')
        if (reason == 'addressed' and not direct and current.mentioned_user_ids
                and current.bot_user_id not in current.mentioned_user_ids):
            await self.call('record', scope, 'skip', 'addressed_to_other')
            return
        target_id = str(decision.get('target_id', current.message_id))
        if target_id not in target_ids:
            raise ValueError('Model selected a message outside this channel context.')
        await self.call('record', scope, action, reason,
                        {'target_id':target_id,'prompt':VERSION,'config':cfg_revision,'source':decision_source,
                         'reason_source':decision.get('reason_source','local'),'same_member_followup':plan.followup,
                         'visible_messages':len(target_ids),'judge_input_estimate':judge_input})
        if action != 'reply':
            return  # wait is reconsidered only after new input; no timer loop of model calls.
        if state.sequence != snapshot or not self.allowed(scope) or revision(self.config(scope))!=cfg_revision:
            await self.call('record', scope, 'cancelled', 'new_messages')
            return
        if cfg['mode'] == 'observe':
            await self.call('record', scope, 'observed', reason, {'memory_ids': [row['id'] for row in memories]})
            return
        # Only after deciding to reply do we pay for vision, full persona/history and deep recall.
        image_status = ('recognized' if current.image_descriptions else 'not_provided' if not current.images
                        else 'disabled' if not cfg.get('vision_enabled') else 'unavailable')
        if self.vision and cfg.get('vision_enabled') and current.images and not current.image_descriptions:
            try:
                descriptions = await self.vision.describe(current,cfg)
                image_status = 'recognized' if any(descriptions) else 'failed'
            except BudgetExceeded:
                raise
            except Exception:
                descriptions = ()
                image_status = 'failed'
            if any(contains_secret(text) for text in descriptions):
                descriptions = ()
                image_status = 'redacted'
            if state.sequence!=snapshot:
                return
            current=replace(current,image_descriptions=descriptions)
            state.messages=deque((current if row.message_id==current.message_id else row for row in state.messages),maxlen=100)
            await self.memory.ingest(current,False)
            if self.persona:
                await self.persona.observe(current)
        prefix=self.model_prefix(scope,state,cfg)
        persona_epoch=None
        if self.persona and self.persona.enabled(scope) and self.persona.repository.path.exists():
            persona_state=await self.persona.status(scope)
            persona_epoch=persona_state['epoch']
            learned=[{'slot':row['slot'],'text':row['text']} for row in persona_state['notes']]
            if learned:
                prefix[0]['content']+='\nOptional learned habits JSON (subordinate to the fixed core and configured expression_style):\n'+json.dumps(learned,ensure_ascii=False,sort_keys=True)
        epoch=await self.memory_epoch(scope)
        payload={'memory':[],'shallow_memory':[],'memory_layer':'shallow',
                 'seconds_since_bot':int(now-state.last_sent),'timezone':cfg['timezone'],
                 'now':datetime.now(ZoneInfo(cfg['timezone'])).isoformat(),
                 'max_reply_characters':cfg['max_reply_chars'],'max_reply_tokens':cfg['reply_max_tokens'],
                 'allow_message_breaks':cfg['reply_burst_enabled'],
                 'vision':{'enabled':bool(cfg.get('vision_enabled')),'message_id':current.message_id,
                           'attachments':len(current.images),'status':image_status},
                 'participation':{'style':cfg['participation_style'],'followup':plan.followup,'recent_messages':plan.activity}}
        if self.persona:
            payload['persona_learning']=await self.persona.guidance(scope)
        try:
            if epoch is not None:
                payload['shallow_memory']=await self.memory.shallow(scope)
                if needs_deep_memory(current.content,decision):
                    payload['memory']=await self.memory.deep(scope,current.content,current.author_id)
                    payload['memory_layer']='deep'
        except Exception:
            pass
        async def still_current():
            latest_epoch = await self.memory_epoch(scope)
            if persona_epoch is not None and (await self.persona.status(scope))['epoch']!=persona_epoch:
                return False
            return (state.sequence == snapshot and latest_epoch == epoch and self.allowed(scope)
                    and revision(self.config(scope)) == cfg_revision
                    and revision(self.memory.config(scope)) == memory_revision and await state.can_send())
        async def record_tool(name,status):
            await self.call('record',scope,'tool',name,{'status':status})
        thinking=thinking_plan(current.content,cfg,decision)
        if thinking.effort!='none':
            if hasattr(self.model,'budget') and (await self.model.budget())['remaining_cny']<.1:
                thinking=ThinkingPlan(reason='preserve_budget')
            await self.call('record',scope,'thinking',thinking.reason,{'effort':thinking.effort})
        try:
            async with asyncio.timeout(90 if thinking.effort!='none' else 45):
                response=await self.replies.generate(prefix,self.model_task,payload,cfg,scope=scope,current=current,
                    still_current=still_current,record_tool=record_tool,thinking=thinking)
        except TimeoutError:
            await self.call('record',scope,'cancelled','reply_timeout')
            return
        if response is None:
            await self.call('record',scope,'cancelled','context_changed_during_tools')
            return
        parts=reply_parts(response.strip(),cfg)
        response='\n'.join(parts)
        if not response or len(response) > cfg['max_reply_chars'] or len(response.encode('utf-16-le')) > 3800:
            await self.call('record', scope, 'cancelled', 'empty_or_overlong_reply')
            return
        if not await still_current():
            await self.call('record', scope, 'cancelled', 'context_or_permissions_changed')
            return
        previous=[row.content.strip() for row in state.messages if row.is_bot][-len(parts):]
        if previous==parts:
            await self.call('record', scope, 'skip', 'duplicate_reply')
            return
        batch = hashlib.sha256((scope + current.message_id + str(snapshot) + cfg_revision).encode()).hexdigest()
        delivery = await self.call('reserve', scope, batch, response, not direct)
        if not delivery:
            return
        await self.call('prepare_parts',delivery,parts)
        await self.call('finish', delivery, 'sending')
        sent=[]
        try:
            for index,part in enumerate(parts):
                if index:
                    await self.pause(random.uniform(.6,1.2))
                # New input, permission/config changes or forgetting stop the remaining burst.
                if not await still_current():
                    for pending in range(index,len(parts)):
                        await self.call('finish_part',delivery,pending,'cancelled')
                    await self.call('record',scope,'cancelled','burst_interrupted',{'sent_parts':len(sent)})
                    break
                await self.call('finish_part',delivery,index,'sending')
                if not await still_current():
                    for pending in range(index,len(parts)):
                        await self.call('finish_part',delivery,pending,'cancelled')
                    break
                try:
                    sent_event=await state.send(part,target_id)
                except BaseException:
                    await self.call('finish_part',delivery,index,'unknown')
                    raise
                await self.call('finish_part',delivery,index,'sent',sent_event.message_id)
                sent.append(sent_event)
                state.last_sent=time.monotonic()
                state.last_author=current.author_id
                state.messages.append(sent_event)
                try:
                    await self.memory.ingest(sent_event,False)
                except Exception:
                    logger.warning('Could not retain sent chat message')
        except BaseException:
            await self.call('finish', delivery, 'unknown')
            raise
        await self.call('finish',delivery,'sent' if sent else 'cancelled',sent[-1].message_id if sent else None)
        if self.persona and payload.get('persona_learning',{}).get('ask_for_feedback') and any(mark in row.content for row in sent for mark in ('?','？')):
            await self.persona.asked(scope)

    async def share_discovery(self,scope,text,*,sequence,config_revision,batch):
        """The only discovery delivery path; reuse channel quotas and durable send guards."""
        state=self.channels.get(scope)
        async def current():
            if self.closing or state is None or state.sequence!=sequence or not self.allowed(scope):
                return False
            cfg=self.config(scope)
            return (cfg['mode']=='participate' and cfg.get('surf_enabled') and cfg['web_enabled']
                    and revision(cfg)==config_revision and not self.quiet(cfg)
                    and state.processed>=state.sequence and (state.task is None or state.task.done()) and await state.can_send())
        if not await current():
            return 'cancelled'
        cfg=self.config(scope)
        if (not text.strip() or contains_secret(text) or len(text)>cfg['max_reply_chars']
                or len(text.encode('utf-16-le'))>3800
                or not await self.call('allowed',scope,cfg['hourly_limit'],cfg['daily_limit'])):
            return 'cancelled'
        delivery=await self.call('reserve',scope,'surf:'+batch,text,True)
        if not delivery:
            return 'cancelled'
        await self.call('finish',delivery,'sending')
        if not await current():
            await self.call('finish',delivery,'cancelled')
            return 'cancelled'
        try:
            sent=await state.send(text,state.messages[-1].message_id)
        except BaseException:
            await self.call('finish',delivery,'unknown')
            raise
        await self.call('finish',delivery,'sent',sent.message_id)
        state.messages.append(sent)
        state.last_sent=time.monotonic()
        state.last_author=''
        try:
            await self.memory.ingest(sent,False)
        except Exception:
            logger.warning('Could not retain shared discovery')
        return 'shared'

    async def memory_epoch(self, scope):
        if not self.memory.has_store:
            return 0
        try:
            return await self.memory.epoch(scope)
        except Exception:
            logger.warning('Memory unavailable; using current channel context only')
            return None

    @staticmethod
    def model_task(instruction, payload):
        return {'role': 'user', 'content': instruction + '\nSupplemental context JSON:\n' + json.dumps(payload, ensure_ascii=False, sort_keys=True)}

    @staticmethod
    def model_prefix(scope, state, cfg):
        """Keep history append-only between bounded, coarse window rotations."""
        visible = [message for message in state.messages if message.author_id not in cfg['excluded_users']]
        start = next((i for i, row in enumerate(visible) if row.message_id == state.context_anchor), 0)
        rows = [row.context() for row in visible[start:]]
        def size(row):
            return len(row['text'])+sum(len(text) for text in row.get('image_observations',[]))
        total = sum(size(row) for row in rows)
        if len(rows) > cfg['context_messages'] or total > cfg['context_chars']:
            target_count = max(1, cfg['context_messages'] * 3 // 4)
            target_chars = cfg['context_chars'] * 3 // 4
            while len(rows) > 1 and (len(rows) > target_count or total > target_chars):
                total -= size(rows.pop(0))
        if rows:
            observations = rows[-1].get('image_observations',[])
            remaining = cfg['context_chars']-sum(len(text) for text in observations)
            rows[-1]['text'] = rows[-1]['text'][-max(0,remaining):] if remaining>0 else ''
            state.context_anchor = rows[0]['message_id']
        identity = {key: cfg[key] for key in ('name', 'persona', 'interests', 'aliases', 'model')}
        identity['expression_style']=cfg.get('expression_style','natural')
        identity['bot_user_id'] = next((message.bot_user_id for message in reversed(visible) if message.bot_user_id), '')
        identity.update(scope=scope, prompt_version=VERSION)
        prefix = [{'role': 'system', 'content': COMMON + '\n' + expression_prompt(cfg)
                   + '\nIdentity JSON:\n' + json.dumps(identity, ensure_ascii=False, sort_keys=True)}]
        prefix.extend({'role': 'assistant' if row['role'] == 'assistant' else 'user',
                       'content': json.dumps(row, ensure_ascii=False, sort_keys=True)} for row in rows)
        return prefix

    async def edited(self, event):
        if self.vision:
            self.vision.invalidate(event.scope.key,event.message_id)
        if self.persona:
            await self.persona.forget(event.scope.key,identifier=event.message_id,deleted=False)
        if contains_secret(event.content) or event.author_id in self.config(event.scope.key)['excluded_users']:
            await self.deleted(event.scope.key, event.message_id)
            return
        state = self.channels.get(event.scope.key)
        if state:
            state.messages = deque((event if row.message_id == event.message_id else row for row in state.messages), maxlen=100)
            state.sequence += 1
            state.processed = state.sequence
            state.wake.set()
        await self.memory.ingest(event, self.config(event.scope.key)['mode'] == 'observe')
        if self.persona:
            await self.persona.observe(event)

    async def deleted(self, scope, message_id):
        if self.vision:
            self.vision.invalidate(scope,message_id)
        if self.persona:
            await self.persona.forget(scope,identifier=message_id)
        state = self.channels.get(scope)
        if state:
            state.messages = deque((row for row in state.messages if row.message_id != message_id), maxlen=100)
            state.sequence += 1
            state.processed = state.sequence
            state.wake.set()
        await self.memory.delete_message(scope, message_id)

    async def clear_context(self, scope):
        if self.vision:
            self.vision.invalidate(scope)
        state = self.channels.get(scope)
        if state:
            state.sequence += 1
            state.processed = state.sequence
            state.messages.clear()
            state.wake.set()
        if self.repository.path.exists():
            await self.call('redact', scope)

    async def close(self):
        if self.closing:
            return
        self.closing = True
        tasks = [state.task for state in self.channels.values() if state.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
