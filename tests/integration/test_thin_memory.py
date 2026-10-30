"""Cheap decision input and selective shallow/deep memory loading."""
import copy
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot_app.features.chat.judgement import judge_messages,estimated_tokens,normalize
from bot_app.features.chat.participation import ParticipationPlan
from bot_app.features.chat.settings import DEFAULTS
from bot_app.features.chat.storage import ChatRepository
from bot_app.features.chat.service import ChatService,ChannelState
from bot_app.features.memory.api import MemoryService
from bot_app.features.memory.storage import MemoryRepository
from bot_app.features.memory.settings import DEFAULTS as MEMORY_DEFAULTS
from bot_app.shared.identity import Scope,MessageEvent


def event(identifier='100',text='这个游戏有点好玩',direct=False):
    return MessageEvent(Scope('1','2'),identifier,'3','member',text,time.time(),direct=direct,bot_user_id='99')


class ThinDecisionTests(unittest.TestCase):
    def test_thin_context_discards_full_persona_history_and_raw_snowflakes(self):
        state=ChannelState(messages=deque(event(str(i),text='随便聊聊'+str(i)+'详细内容'*300) for i in range(100,150)))
        cfg=dict(DEFAULTS,persona='简短群友\n'+'不应进入判断的大段设定'*300)
        messages,targets=judge_messages(state,cfg,[{'content':'最近讨论游戏'}],ParticipationPlan(1,4,False,3))
        self.assertNotIn('不应进入判断的大段设定',str(messages))
        self.assertLessEqual(estimated_tokens(''.join(row['content'] for row in messages)),320)
        self.assertIn('149',targets.values())
        self.assertLess(len(targets),5)

    def test_compact_decision_maps_only_visible_indexes_to_real_message_ids(self):
        decision=normalize({'a':'r','r':'useful','i':2,'d':1,'t':1},{2:'100'})
        self.assertEqual(decision['target_id'],'100')
        self.assertEqual(decision['memory'],'deep')
        with self.assertRaises(ValueError):
            normalize({'a':'r','r':'useful','i':99},{2:'100'})


class LayeredMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg=dict(copy.deepcopy(DEFAULTS),enabled=True,channels=['2'],mode='participate',cooldown_seconds=0)
        self.model=SimpleNamespace(complete=AsyncMock(),embed=AsyncMock())
        self.memory=MemoryService(MemoryRepository(Path(self.tmp.name)/'memory.db'),self.model,
            lambda _:dict(MEMORY_DEFAULTS,enabled=True),lambda _:True)
        self.chat=ChatService(ChatRepository(Path(self.tmp.name)/'chat.db'),self.model,self.memory,lambda _:self.cfg)
        self.addAsyncCleanup(self.memory.close); self.addAsyncCleanup(self.chat.close)
        self.state=ChannelState(messages=deque([event()]),sequence=1)
        self.state.can_send=AsyncMock(return_value=True)
        self.state.send=AsyncMock(return_value=MessageEvent(Scope('1','2'),'200','99','bot','嗯。',time.time(),is_bot=True))
        self.deep_fact={'id':'fact-1','subject':'3','kind':'fact','content':'长期偏好古典音乐。','created':0,'valid_until':None,'attribution':'self_report'}
        self.memory.recall=AsyncMock(return_value=[self.deep_fact])

    async def test_skip_never_retrieves_deep_memory_or_generates_an_answer(self):
        self.model.complete.return_value={'a':'s','r':'unrelated','i':0,'d':0,'t':0}
        await self.chat.round('discord:1:2',self.state,1)
        self.memory.recall.assert_not_awaited()
        self.model.embed.assert_not_awaited()
        self.model.complete.assert_awaited_once()
        self.state.send.assert_not_awaited()

    async def test_casual_reply_uses_shallow_context_and_300_token_output(self):
        self.model.complete.side_effect=[{'a':'r','r':'useful','i':0,'d':0,'t':0},'确实挺有意思。']
        await self.chat.round('discord:1:2',self.state,1)
        self.memory.recall.assert_not_awaited()
        self.assertEqual(self.model.complete.await_args.kwargs['max_tokens'],300)
        self.assertNotIn('长期偏好古典音乐',str(self.model.complete.await_args.args[1]))

    async def test_past_fact_question_loads_only_relevant_deep_records_after_decision(self):
        self.state.messages=deque([event(text='你还记得我喜欢什么音乐吗？')])
        self.model.complete.side_effect=[{'a':'r','r':'useful','i':0,'d':1,'t':0},'你之前说喜欢古典音乐。']
        await self.chat.round('discord:1:2',self.state,1)
        self.memory.recall.assert_awaited_once()
        judge=self.model.complete.await_args_list[0].args[1]
        reply=self.model.complete.await_args_list[1].args[1]
        self.assertNotIn('长期偏好古典音乐',str(judge))
        self.assertIn('长期偏好古典音乐',str(reply))

    async def test_direct_question_can_skip_the_judge_without_loading_unneeded_memories(self):
        self.state.messages=deque([event(text='<@99> 你好',direct=True)])
        self.model.complete.return_value='嗨。'
        await self.chat.round('discord:1:2',self.state,1)
        self.model.complete.assert_awaited_once()
        self.memory.recall.assert_not_awaited()

    async def test_shallow_summary_has_no_embedding_and_expires(self):
        source=event('500',text='最近在聊音乐')
        await self.memory.call('ingest',source)
        await self.memory.call('queue','discord:1:2',batch_size=1,wait_seconds=10,force=True)
        job=await self.memory.call('claim')
        await self.memory.call('apply',job,[],summary='最近群里在聊音乐。')
        self.assertTrue(await self.memory.shallow('discord:1:2'))
        self.model.embed.assert_not_awaited()
        def expire():
            with self.memory.repository.db() as db:
                db.execute('UPDATE memories SET created=?',(time.time()-2*86400,))
        import asyncio
        await asyncio.to_thread(expire)
        self.assertFalse(await self.memory.shallow('discord:1:2'))
