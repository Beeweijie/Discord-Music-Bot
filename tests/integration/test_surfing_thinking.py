"""Sparse autonomous discovery, shared spending pools and bounded reasoning tool loops."""
import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot_app.features.chat.settings import DEFAULTS
from bot_app.features.chat.service import ChatService,ChannelState
from bot_app.features.chat.storage import ChatRepository
from bot_app.features.chat.thinking import thinking_plan,ThinkingPlan
from bot_app.features.chat.reply import ReplyGenerator
from bot_app.features.memory.api import MemoryService
from bot_app.features.memory.storage import MemoryRepository
from bot_app.features.memory.settings import DEFAULTS as MEMORY_DEFAULTS
from bot_app.features.surfing.api import SurfService
from bot_app.features.surfing.storage import SurfRepository
from bot_app.features.surfing.policy import available,canonical_url
from bot_app.shared.identity import MessageEvent,Scope
from bot_app.shared.revisions import revision
from bot_app.shared.llm.deepseek import DeepSeekClient,BudgetExceeded,ThinkingLimitReached


def message(identifier='100',text='这个新游戏看起来还不错',created=None,bot=False):
    return MessageEvent(Scope('1','2'),identifier,'99' if bot else '3','bot' if bot else 'member',text,
                        time.time()-180 if created is None else created,is_bot=bot,bot_user_id='99')


class ThinkingPolicyTests(unittest.TestCase):
    def test_casual_and_search_questions_do_not_need_deeper_reasoning(self):
        for text in ['哈哈哈','最近有什么游戏新闻？','帮我查一下今天的天气']:
            self.assertEqual(thinking_plan(text,DEFAULTS).effort,'none')

    def test_complex_explicit_and_disabled_routes(self):
        self.assertEqual(thinking_plan('证明这个算法为什么正确',DEFAULTS).effort,'low')
        self.assertEqual(thinking_plan('请深度思考这个多约束问题',DEFAULTS).effort,'high')
        self.assertEqual(thinking_plan('请深度思考',dict(DEFAULTS,thinking_mode='off')).effort,'none')
        self.assertEqual(thinking_plan('这段提问涉及很多互相约束的条件，需要兼顾成本和效果',DEFAULTS,{'thinking':'low'}).effort,'low')
        self.assertEqual(thinking_plan('哈哈',DEFAULTS,{'thinking':'high'}).effort,'none')


class ThinkingTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.bodies=[]
        self.finish_reason='stop'
        async def reply(request):
            body=await request.json(); self.bodies.append(body)
            return web.json_response({'choices':[{'message':{'role':'assistant','content':'最终回答',
                'reasoning_content':'private intermediate reasoning','tool_calls':[]},'finish_reason':self.finish_reason}],
                'usage':{'prompt_tokens':20,'completion_tokens':40,'completion_tokens_details':{'reasoning_tokens':30}}})
        app=web.Application(); app.router.add_post('/chat/completions',reply)
        server=TestServer(app); await server.start_server(); self.addAsyncCleanup(server.close)
        self.client=DeepSeekClient(self.tmp.name,key='offline',base_url=str(server.make_url('/')).rstrip('/'))
        self.addAsyncCleanup(self.client.close)

    async def test_thinking_toggle_reasoning_stats_and_final_answer_separation(self):
        answer=await self.client.complete('chat_reply',[{'role':'user','content':'proof'}],scope='discord:1:2',thinking='low')
        self.assertEqual(answer,'最终回答')
        self.assertEqual(self.bodies[0]['thinking'],{'type':'enabled'})
        self.assertEqual(self.bodies[0]['reasoning_effort'],'low')
        usage=(await self.client.usage('discord:1:'))[0]
        self.assertEqual(usage['output_tokens'],40)
        self.assertEqual(usage['reasoning_tokens'],30)
        self.assertAlmostEqual(usage['cost_cny'],(20*2000+40*8000)/1e9)

    async def test_current_tool_loop_preserves_reasoning_without_mutating_history(self):
        original=[{'role':'assistant','content':'earlier public reply'}]
        answer=await self.client.complete('chat_reply',original,scope='discord:1:2',thinking='low',tools=[])
        self.assertEqual(answer['reasoning_content'],'private intermediate reasoning')
        self.assertEqual(self.bodies[0]['messages'][0]['reasoning_content'],'')
        self.assertNotIn('reasoning_content',original[0])

    async def test_surf_pool_is_inside_global_money_and_cannot_overrun(self):
        one=self.client._reserve('discord:1:2','surf_plan','deepseek-flash',0,70_000_000,'surf')
        with self.assertRaises(BudgetExceeded):
            self.client._reserve('discord:2:3','web_search','deepseek-search',0,40_000_000,'surf')
        budget=await self.client.budget()
        self.assertEqual(budget['reserved_cny'],.07)
        self.assertEqual(budget['pools']['surf']['reserved_cny'],.07)
        self.client._finish(one,'failed',{})
        self.assertEqual((await self.client.budget())['pools']['surf']['spent_cny'],.07)

    async def test_bounded_reasoning_has_only_one_fast_fallback(self):
        model=SimpleNamespace(complete=AsyncMock(side_effect=[ThinkingLimitReached('bounded'),'简短回答']))
        guard=AsyncMock(return_value=True)
        trace=AsyncMock()
        generator=ReplyGenerator(model)
        answer=await generator.generate([],lambda instruction,payload:{'role':'user','content':instruction}, {},DEFAULTS,
            scope='discord:1:2',current=message(),still_current=guard,record_tool=trace,thinking=ThinkingPlan('low',1536,'complex'))
        self.assertEqual(answer,'简短回答')
        self.assertEqual([call.kwargs['thinking'] for call in model.complete.await_args_list],['low','none'])
        trace.assert_awaited_once_with('thinking','bounded_fast_fallback')

    async def test_exhausted_reasoning_is_billed_and_never_returned_as_a_partial_answer(self):
        self.finish_reason='length'
        with self.assertRaises(ThinkingLimitReached):
            await self.client.complete('chat_reply',[{'role':'user','content':'proof'}],scope='discord:1:2',thinking='low')
        usage=(await self.client.usage('discord:1:'))[0]
        self.assertEqual(usage['output_tokens'],40)
        self.assertGreater(usage['cost_cny'],0)


class SurfStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo=SurfRepository(Path(self.tmp.name)/'surf.db')
        self.scope='discord:1:2'; self.day='2026-10-31'
        self.now=time.time()

    def add(self,count=20,start=100,scope=None):
        for i in range(start,start+count):
            self.repo.observe(scope or self.scope,str(i))

    def test_requires_new_messages_and_six_hours_between_attempts(self):
        self.add(19)
        self.assertIsNone(self.repo.claim(self.scope,self.day,self.now))
        self.add(1,start=119)
        first=self.repo.claim(self.scope,self.day,self.now)
        self.assertIsNotNone(first)
        self.repo.finish(first,'skipped')
        self.add(start=200)
        self.assertIsNone(self.repo.claim(self.scope,self.day,self.now+6*3600-1))
        second=self.repo.claim(self.scope,self.day,self.now+6*3600)
        self.assertIsNotNone(second)
        self.repo.finish(second,'skipped')
        self.add(start=300)
        self.assertIsNone(self.repo.claim(self.scope,self.day,self.now+12*3600))

    def test_restarts_and_other_channels_cannot_bypass_single_daily_share(self):
        self.add(); first=self.repo.claim(self.scope,self.day,self.now)
        self.assertTrue(self.repo.reserve_share(first,'https://example.com/news','News'))
        self.repo.finish(first,'shared')
        restarted=SurfRepository(self.repo.path)
        self.add(scope='discord:2:3')
        self.assertIsNone(restarted.claim('discord:2:3',self.day,self.now+8*3600))

    def test_ambiguous_sends_are_never_repeated_and_tracking_links_are_deduplicated(self):
        self.add(); first=self.repo.claim(self.scope,self.day,self.now)
        self.repo.reserve_share(first,'https://example.com/news?utm_source=test#section','A title')
        self.repo.retire()
        self.assertTrue(self.repo.seen('https://example.com/news','different title'))
        self.assertTrue(self.repo.seen('https://another.example/news','A title'))
        self.assertEqual(canonical_url('https://example.com/news?utm_source=test#fragment'),'https://example.com/news')


class SurfFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg=dict(copy.deepcopy(DEFAULTS),enabled=True,channels=['2'],mode='participate',surf_enabled=True,web_enabled=True)
        self.model=SimpleNamespace(configured=True,budget_day=lambda:'2026-10-31',budget=AsyncMock(return_value={
            'remaining_cny':.9,'day':'2026-10-31','pools':{'surf':{'remaining_cny':.1}}}),complete=AsyncMock())
        self.web=SimpleNamespace(execute=AsyncMock(return_value={'results':[{
            'url':'https://example.com/news','title':'Interesting game development','summary':'A new game-making tool.','content':'Public source.'}]}))
        self.memory=MemoryService(MemoryRepository(Path(self.tmp.name)/'memory.db'),self.model,
            lambda _:dict(MEMORY_DEFAULTS),lambda _:True)
        self.chat=ChatService(ChatRepository(Path(self.tmp.name)/'chat.db'),self.model,self.memory,lambda _:self.cfg)
        self.surf=SurfService(SurfRepository(Path(self.tmp.name)/'surf.db'),self.model,self.web,self.chat)
        self.surf.started=time.monotonic()-7200
        self.addAsyncCleanup(self.memory.close); self.addAsyncCleanup(self.chat.close); self.addAsyncCleanup(self.surf.close)
        self.scope='discord:1:2'
        self.state=ChannelState(); self.state.messages.append(message())
        self.state.sequence=self.state.processed=1
        self.state.send=AsyncMock(return_value=message('200','看到个有意思的游戏工具。',created=time.time(),bot=True))
        self.state.can_send=AsyncMock(return_value=True)
        self.chat.channels[self.scope]=self.state
        for i in range(100,120):
            await self.surf.call('observe',self.scope,str(i))
        self.model.complete.side_effect=[{'search':True,'query':'new game development tools'},
            {'share':True,'url':'https://example.com/news','text':'刚看到个有意思的游戏工具，感觉可以试试 https://example.com/news'}]

    async def test_eligible_discovery_uses_shared_pool_and_posts_only_once(self):
        await self.surf.tick()
        self.state.send.assert_awaited_once()
        self.assertEqual(self.surf.state,'shared')
        self.assertEqual(self.web.execute.await_args.kwargs['budget_pool'],'surf')
        self.assertTrue(all(call.kwargs['budget_pool']=='surf' for call in self.model.complete.await_args_list))
        await self.surf.tick()
        self.state.send.assert_awaited_once()

    async def test_new_messages_cancel_an_inflight_discovery(self):
        async def search(*args,**kwargs):
            self.state.sequence+=1
            return {'results':[{'url':'https://example.com/news','title':'News'}]}
        self.web.execute.side_effect=search
        await self.surf.tick()
        self.state.send.assert_not_awaited()
        self.assertEqual(self.model.complete.await_count,1)

    async def test_no_good_item_means_silence(self):
        self.model.complete.side_effect=[{'search':False}]
        await self.surf.tick()
        self.web.execute.assert_not_awaited(); self.state.send.assert_not_awaited()

    async def test_low_budget_or_sleeping_channel_causes_no_model_requests(self):
        self.model.budget.return_value['remaining_cny']=.19
        await self.surf.tick()
        self.model.complete.assert_not_awaited()
        self.model.budget.return_value['remaining_cny']=.9
        self.state.messages.clear(); self.state.messages.append(message(created=time.time()-3600))
        await self.surf.tick()
        self.model.complete.assert_not_awaited()

    async def test_warmup_recent_activity_and_quiet_gap_are_mandatory(self):
        self.surf.started=time.monotonic()
        await self.surf.tick(); self.model.complete.assert_not_awaited()
        self.surf.started=time.monotonic()-7200
        self.state.messages.clear(); self.state.messages.append(message(created=time.time()-30))
        await self.surf.tick(); self.model.complete.assert_not_awaited()
        self.state.messages.clear(); self.state.messages.append(message(text='<@88> 你怎么看'))
        await self.surf.tick(); self.model.complete.assert_not_awaited()

    async def test_recheck_after_delivery_reservation_prevents_stale_send(self):
        original=self.chat.call
        async def call(method,*args,**kwargs):
            result=await original(method,*args,**kwargs)
            if method=='finish' and args[1]=='sending':
                self.state.sequence+=1
            return result
        self.chat.call=call
        await self.surf.tick()
        self.state.send.assert_not_awaited()

    async def test_fabricated_or_extra_source_links_cannot_be_shared(self):
        self.model.complete.side_effect=[{'search':True,'query':'game tools'},
            {'share':True,'url':'https://example.com/news','text':'看这个 https://evil.example/ https://example.com/news'}]
        await self.surf.tick()
        self.state.send.assert_not_awaited()
