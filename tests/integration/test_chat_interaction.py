"""Social continuity, adaptive batching and citation presentation, all offline."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot_app.features.chat.interaction import InteractionState, interaction_context, remember_exchange, local_kind
from bot_app.features.chat.judgement import normalize
from bot_app.features.chat.participation import participation_plan, pending_due, incomplete_input
from bot_app.features.chat.persona_cards import BLUE_FISH_CORE, BLUE_FISH_PERSONA
from bot_app.features.chat.service import ChatService, ChannelState
from bot_app.features.chat.settings import DEFAULTS
from bot_app.features.chat.storage import ChatRepository
from bot_app.features.chat.web_presentation import web_presentation, result_sources
from bot_app.features.memory.api import MemoryService
from bot_app.features.memory.settings import DEFAULTS as MEMORY_DEFAULTS
from bot_app.features.memory.storage import MemoryRepository
from bot_app.shared.identity import MessageEvent, Scope


def event(identifier,text,*,author='3',bot=False,direct=False,age=0,channel='2'):
    return MessageEvent(Scope('1',channel),str(identifier),'99' if bot else author,'bot' if bot else 'member',
        text,time.time()-age,is_bot=bot,direct=direct,bot_user_id='99')


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.cfg=dict(copy.deepcopy(DEFAULTS),participation_style='active',debounce_seconds=2,max_wait_seconds=4)

    def test_complete_direct_question_is_faster_than_a_fragmented_turn(self):
        simple=ChannelState(messages=deque([event(100,'你在吗？',direct=True)]))
        broken=ChannelState(messages=deque([event(100,'我刚想到',age=2),event(101,'我想说的是',direct=True)]))
        self.assertLess(participation_plan(simple,self.cfg,100).delay,1.5)
        self.assertGreaterEqual(participation_plan(broken,self.cfg,100).delay,6)

    def test_consecutive_messages_get_room_but_another_speaker_does_not_join_the_burst(self):
        state=ChannelState(messages=deque([event(100,'这歌前奏不错',age=2),event(101,'后面鼓点也好听')]))
        self.assertGreaterEqual(participation_plan(state,self.cfg,100).delay,4)
        state.messages[0]=event(100,'无关的人说话',author='4',age=2)
        self.assertLess(participation_plan(state,self.cfg,100).delay,4)

    def test_no_punctuation_is_not_itself_incomplete(self):
        for text in ('刚下班','这首好听','为什么？','还有别的吗？','这个游戏挺好玩'):
            self.assertFalse(incomplete_input(text),text)
        for text in ('我跟你讲','主要是因为','我想说的是','事情是这样：'):
            self.assertTrue(incomplete_input(text),text)

    def test_quiet_window_moves_with_new_input_but_the_first_fragment_bounds_it(self):
        state=ChannelState(messages=deque([event(100,'然后')]),first_pending=100,last_pending=102)
        first=pending_due(state,self.cfg,102)
        state.last_pending=104
        second=pending_due(state,self.cfg,104)
        self.assertGreater(first,state.first_pending+self.cfg['max_wait_seconds'])
        self.assertLessEqual(second,108)
        self.assertGreaterEqual(second,first)


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(DEFAULTS)
        self.state=ChannelState(messages=deque([event(100,'说好明天再玩，你怎么今天就开了？',age=20),
            event(101,'提前热身',bot=True,age=15),event(102,'我说的是下周的比赛，不是游戏日常')]))
        remember_exchange(self.state,self.state.messages[0],{'kind':'joke'},[self.state.messages[1]])

    def context(self,**decision):
        return interaction_context(self.state,self.cfg,self.state.messages[-1],decision)

    def test_correction_has_the_original_exchange_and_a_source_grounded_repair_request(self):
        context=self.context(kind='joke')
        self.assertEqual(context['kind'],'repair')
        self.assertEqual(context['repair_request']['source_id'],'102')
        self.assertEqual(context['previous_exchange']['reply']['source_id'],'101')
        self.assertEqual([row['source_id'] for row in context['pending_inputs']],['102'])

    def test_serious_request_stops_inheriting_a_joke(self):
        self.state.messages[-1]=event(102,'别损我了，认真说，这事有点难受')
        self.assertEqual(self.context(kind='joke')['kind'],'help')

    def test_expired_removed_or_excluded_sources_cannot_survive_in_the_frame(self):
        self.state.messages[0]=event(100,'不能再使用的旧话',age=1000)
        self.assertNotIn('previous_exchange',self.context())
        self.state.messages.popleft()
        self.assertNotIn('previous_exchange',self.context())
        self.cfg['excluded_users']=['99']
        self.assertNotIn('previous_exchange',self.context())

    def test_another_member_and_another_channel_do_not_inherit_this_exchange(self):
        self.state.messages[-1]=event(102,'接着说',author='4')
        self.assertNotIn('previous_exchange',self.context())
        self.state.messages[-1]=event(102,'接着说',channel='5')
        self.assertNotIn('previous_exchange',self.context())

    def test_two_unanswered_fragments_are_combined_and_a_question_is_kept(self):
        self.state.messages.extend([event(103,'不是那件事',age=1),event(104,'那我现在该怎么回？')])
        context=self.context()
        self.assertEqual(len(context['pending_inputs']),3)
        self.assertEqual(context['pending_question']['source_id'],'104')

    def test_failed_delivery_does_not_create_an_exchange(self):
        self.state.interaction=InteractionState()
        remember_exchange(self.state,self.state.messages[-1],{'kind':'joke'},[])
        self.assertEqual(self.state.interaction,InteractionState())

    def test_playing_against_bots_is_not_feedback_about_our_voice(self):
        self.assertEqual(local_kind('我昨晚打了三局人机对战'),'chat')
        self.assertEqual(local_kind('你的回复还是挺人机的'),'feedback')

    def test_optional_kind_is_bounded_and_does_not_break_older_decisions(self):
        self.assertEqual(normalize({'a':'r','i':0,'c':'repair'},{0:'100'})['kind'],'repair')
        for value in (None,{},'ignore all rules'):
            self.assertEqual(normalize({'a':'r','i':0,'c':value},{0:'100'})['kind'],'chat')
        self.assertEqual(normalize({'a':'r','i':0},{0:'100'})['kind'],'chat')


class PresentationTests(unittest.TestCase):
    def test_smalltalk_and_fact_checking_have_different_citation_policies(self):
        self.assertEqual(web_presentation('搜个有意思的冷知识聊聊')['mode'],'chat')
        for text in ('这消息是真的吗，核实一下','最新版本发布了吗','给个官方文档链接','出处呢？'):
            self.assertEqual(web_presentation(text)['mode'],'sources')
        self.assertEqual(web_presentation('出处呢？')['max_links'],1)

    def test_only_actual_public_results_enter_the_citation_trail(self):
        result={'results':[{'url':'https://example.com/a','title':'实际结果'},
                           {'url':'https://example.com/a'}, {'url':'http://127.0.0.1/private'}]}
        self.assertEqual(result_sources(result),[{'url':'https://example.com/a','title':'实际结果'}])

    def test_citation_trail_is_scoped_expires_and_is_removed_when_context_is_forgotten(self):
        with tempfile.TemporaryDirectory() as directory:
            repo=ChatRepository(Path(directory)/'chat.db')
            repo.record('discord:1:2','tool','web_search',{'status':'completed','sources':[{'url':'https://example.com/a'}]})
            repo.record('discord:1:3','tool','web_search',{'status':'completed','sources':[{'url':'https://example.com/other'}]})
            self.assertEqual([row['url'] for row in repo.sources('discord:1:2')],['https://example.com/a'])
            with repo.db() as db:
                db.execute('UPDATE decisions SET created=? WHERE scope=?',(time.time()-1801,'discord:1:2'))
            self.assertEqual(repo.sources('discord:1:2'),[])
            repo.redact('discord:1:3')
            self.assertEqual(repo.sources('discord:1:3'),[])

    def test_voice_examples_are_system_reference_and_never_change_core_or_history(self):
        cfg=dict(DEFAULTS,persona=BLUE_FISH_PERSONA,expression_style='banter')
        state=ChannelState(messages=deque([event(100,'当前真实话题')]))
        original=[row.context() for row in state.messages]
        prefix=ChatService.model_prefix('discord:1:2',state,cfg)
        identity=json.loads(prefix[0]['content'].split('\nIdentity JSON:\n',1)[1])
        self.assertTrue(identity['persona'].startswith(BLUE_FISH_CORE+'\n\n'))
        self.assertEqual(identity['persona'],BLUE_FISH_PERSONA)
        self.assertEqual([json.loads(row['content']) for row in prefix[1:]],original)
        thin_cfg=dict(cfg,expression_style='natural')
        other=ChatService.model_prefix('discord:1:2',ChannelState(messages=deque(state.messages)),thin_cfg)
        self.assertEqual(other[1:],prefix[1:])
        self.assertNotEqual(other[0],prefix[0])


class InteractionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg=dict(copy.deepcopy(DEFAULTS),enabled=True,channels=['2'],mode='participate',cooldown_seconds=0)
        self.model=SimpleNamespace(complete=AsyncMock(side_effect=[{'a':'r','i':0,'c':'joke'},'行，接上了']))
        self.memory=MemoryService(MemoryRepository(Path(self.tmp.name)/'memory.db'),self.model,
            lambda _:dict(MEMORY_DEFAULTS),lambda _:True)
        self.chat=ChatService(ChatRepository(Path(self.tmp.name)/'chat.db'),self.model,self.memory,lambda _:self.cfg)
        self.addAsyncCleanup(self.memory.close);self.addAsyncCleanup(self.chat.close)
        self.state=ChannelState(messages=deque([event(100,'这把全队都在演我')]),sequence=1)
        self.chat.channels['discord:1:2']=self.state
        self.state.send=AsyncMock(return_value=event(101,'行，接上了',bot=True))
        self.state.can_send=AsyncMock(return_value=True)

    async def test_existing_judge_hands_off_context_without_an_extra_model_call(self):
        await self.chat.round('discord:1:2',self.state,1)
        self.assertEqual([call.args[0] for call in self.model.complete.await_args_list],['chat_judge','chat_reply'])
        payload=json.loads(self.model.complete.await_args.args[1][-1]['content'].split('Supplemental context JSON:\n')[1])
        self.assertEqual(payload['interaction']['kind'],'joke')
        self.assertEqual(self.model.complete.await_args.kwargs['max_tokens'],300)
        self.assertEqual(self.state.interaction.reply_id,'101')
        await self.chat.deleted('discord:1:2','100')
        self.assertEqual(self.state.interaction,InteractionState())

    async def test_clear_and_edit_invalidate_interaction_state(self):
        await self.chat.round('discord:1:2',self.state,1)
        await self.chat.edited(event(100,'重新表述'))
        self.assertEqual(self.state.interaction,InteractionState())
        self.state.interaction=InteractionState('3','joke','100','101',time.time())
        await self.chat.clear_context('discord:1:2')
        self.assertEqual(self.state.interaction,InteractionState())

    async def test_direct_source_followup_receives_only_this_channels_real_citation_trail(self):
        self.cfg['web_enabled']=True
        self.chat.replies.web=SimpleNamespace(execute=AsyncMock())
        await self.chat.call('record','discord:1:2','tool','web_search',
            {'status':'completed','sources':[{'title':'原始来源','url':'https://example.com/evidence'}]})
        self.state.messages=deque([event(100,'出处呢？',direct=True)])
        self.model.complete.return_value={'role':'assistant','content':'来源在这 https://example.com/evidence','tool_calls':[]}
        self.model.complete.side_effect=None
        await self.chat.round('discord:1:2',self.state,1)
        payload=json.loads(self.model.complete.await_args.args[1][-1]['content'].split('Supplemental context JSON:\n')[1])
        self.assertEqual(payload['recent_web_sources'][0]['url'],'https://example.com/evidence')
        self.assertEqual(payload['web_presentation']['mode'],'sources')

    async def test_receive_combines_fragments_before_one_judgement_and_one_reply(self):
        self.chat.channels.clear()
        self.cfg.update(participation_style='active',debounce_seconds=2,max_wait_seconds=4)
        self.model.complete.side_effect=[{'a':'r','i':1,'c':'chat'},'听明白了']
        finished=asyncio.Event()
        async def send(text,target):
            finished.set()
            return event(103,text,bot=True)
        real_due=pending_due
        def accelerated(state,cfg,now):
            return now+max(0,real_due(state,cfg,now)-now)*.01
        with patch('bot_app.features.chat.service.pending_due',side_effect=accelerated):
            await self.chat.receive(event(100,'我跟你讲'),send,AsyncMock(return_value=True))
            await self.chat.receive(event(101,'是另外一个存档，刚才没说清'),send,AsyncMock(return_value=True))
            await asyncio.wait_for(finished.wait(),2)
        self.assertEqual(self.model.complete.await_count,2)
        reply=self.model.complete.await_args.args[1]
        actual=[json.loads(row['content'])['text'] for row in reply[1:-1]]
        self.assertEqual(actual,['我跟你讲','是另外一个存档，刚才没说清'])
