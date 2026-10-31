"""Web tool execution and active group participation without real credentials or Discord."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot_app.features.chat.participation import participation_plan,response_nudge
from bot_app.features.chat.service import ChannelState,ChatService
from bot_app.features.chat.reply import ReplyGenerator
from bot_app.features.chat.settings import DEFAULTS,validate
from bot_app.features.chat.storage import ChatRepository
from bot_app.features.memory.api import MemoryService
from bot_app.features.memory.storage import MemoryRepository
from bot_app.features.memory.settings import DEFAULTS as MEMORY_DEFAULTS
from bot_app.features.web_search.api import WebSearchService,public_url
from bot_app.shared.identity import Scope,MessageEvent
from bot_app.shared.llm.deepseek import DeepSeekClient,ModelUnavailable


def event(text='这个游戏怎么样？',direct=False,identifier='100',author='3'):
    return MessageEvent(Scope('1','2'),identifier,author,'member',text,time.time(),direct=direct,bot_user_id='99')


class ParticipationTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(DEFAULTS)
        self.cfg.update(participation_style='active',cooldown_seconds=8,debounce_seconds=2,max_wait_seconds=4)
        self.state=ChannelState(); self.state.messages.append(event())

    def test_unmentioned_questions_are_considered_without_90_second_cooldown(self):
        plan=participation_plan(self.state,self.cfg,100)
        self.assertEqual(plan.interval,4)
        self.assertGreaterEqual(plan.delay,2)
        self.assertLessEqual(plan.delay,4)

    def test_followups_have_a_shorter_interval(self):
        self.state.last_sent=98; self.state.last_author='3'
        plan=participation_plan(self.state,self.cfg,100)
        self.assertTrue(plan.followup)
        self.assertEqual(plan.interval,2)

    def test_active_followup_survives_a_few_minutes_but_expires(self):
        self.state.last_sent=100; self.state.last_author='3'
        self.assertTrue(participation_plan(self.state,self.cfg,220).followup)
        self.assertTrue(participation_plan(self.state,self.cfg,940).followup)
        self.assertFalse(participation_plan(self.state,self.cfg,1001).followup)
        self.cfg['participation_style']='balanced'
        self.assertFalse(participation_plan(self.state,self.cfg,220).followup)

    def test_other_members_break_the_same_member_followup_hint(self):
        self.state.last_sent=100; self.state.last_author='3'
        self.state.messages=deque([event(author='4'),event(author='3',identifier='101')])
        self.assertFalse(participation_plan(self.state,self.cfg,220).followup)

    def test_explicit_nudges_require_our_own_recent_uninterrupted_exchange(self):
        now=time.time()
        bot=MessageEvent(Scope('1','2'),'99','99','bot','名字被拿来玩梗了。',now-1320,is_bot=True)
        self.state.messages=deque([bot,event('有没有可能你名字就来自这个梗',identifier='100'),event('回一下？',identifier='101')])
        self.state.last_author='3'
        self.assertTrue(response_nudge(self.state,self.cfg))
        self.state.messages[0]=MessageEvent(Scope('1','2'),'99','99','bot','名字被拿来玩梗了。',now-2700,is_bot=True)
        self.assertTrue(response_nudge(self.state,self.cfg))
        self.state.messages[-1]=event('?',identifier='101')
        self.assertFalse(response_nudge(self.state,self.cfg))
        self.state.messages[0]=bot
        self.state.messages[-1]=event('回一下？',identifier='101')
        self.state.messages.insert(2,event('其他群友的话',author='4',identifier='102'))
        self.assertFalse(response_nudge(self.state,self.cfg))
        self.state.messages.remove(self.state.messages[2])
        self.state.messages[-1]=event('<@88> 回一下？',identifier='101')
        self.assertFalse(response_nudge(self.state,self.cfg))
        self.state.messages=deque([event('回一下？')])
        self.assertFalse(response_nudge(self.state,self.cfg))

    def test_busy_channels_get_more_space_but_direct_calls_stay_fast(self):
        self.state.messages=deque(event('大家聊聊看',identifier=str(i)) for i in range(100,108))
        self.assertEqual(participation_plan(self.state,self.cfg,100).interval,12)
        self.state.messages.append(event(direct=True,identifier='109'))
        self.assertEqual(participation_plan(self.state,self.cfg,100).interval,0)

    def test_balanced_policy_retains_configured_interval(self):
        self.cfg.update(participation_style='balanced',cooldown_seconds=90)
        self.assertEqual(participation_plan(self.state,self.cfg,100).interval,90)

    def test_policy_configuration_is_validated(self):
        validate(self.cfg)
        for key,value in [('web_max_calls',0),('web_enabled','true'),('participation_style','unbounded'),('expression_style','unbounded')]:
            invalid=dict(self.cfg,**{key:value})
            with self.assertRaises(ValueError):
                validate(invalid)

    def test_expression_style_is_optional_and_supports_a_channel_override(self):
        from bot_app.features.chat.settings import for_channel
        cfg=validate(dict(self.cfg,expression_style='banter',channel_overrides={'2':{'expression_style':'natural'}}))
        self.assertEqual(cfg['expression_style'],'banter')
        self.assertEqual(for_channel(cfg,'2')['expression_style'],'natural')
        self.assertEqual(for_channel(cfg,'3')['expression_style'],'banter')


class WebServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_is_bounded_scoped_and_cached(self):
        model=SimpleNamespace(web_request=AsyncMock(return_value={'results':[
            {'url':'https://example.com/source','title':'Source','summary':'evidence','content':'x'*12000},
            {'url':'http://127.0.0.1/private','title':'private'}]}))
        service=WebSearchService(model)
        first=await service.execute('web_search',{'query':'public facts'},scope='discord:1:2',daily_limit=10)
        second=await service.execute('web_search',{'query':'public facts'},scope='discord:1:2',daily_limit=10)
        self.assertEqual(first,second)
        self.assertEqual(len(first['results']),1)
        self.assertLessEqual(len(first['results'][0]['content']),1800)
        self.assertEqual(model.web_request.await_count,1)
        await service.execute('web_search',{'query':'public facts'},scope='discord:1:3',daily_limit=10)
        self.assertEqual(model.web_request.await_count,2)

    async def test_credentials_private_urls_and_member_ids_are_not_searched(self):
        model=SimpleNamespace(web_request=AsyncMock())
        service=WebSearchService(model)
        for query in ['DEEPSEEK_API_KEY=credential','member 1565698590867718176','<@123> info']:
            with self.assertRaises(ValueError):
                await service.execute('web_search',{'query':query},scope='discord:1:2',daily_limit=10)
        for url in ['file:///secret','http://127.0.0.1','http://192.168.1.1/a','http://localhost/a',
                    'https://user:pass@example.com','https://example.com/?token=secret','http://metadata.internal/a']:
            with self.subTest(url=url),self.assertRaises(ValueError):
                public_url(url)
        model.web_request.assert_not_awaited()


def tool(name,arguments,identifier='call-1'):
    return {'role':'assistant','content':None,'tool_calls':[{'id':identifier,'type':'function','function':{
        'name':name,'arguments':json.dumps(arguments)}}]}


class ReplyToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg=dict(DEFAULTS,web_enabled=True,web_max_calls=2)
        self.model=SimpleNamespace(complete=AsyncMock())
        self.web=SimpleNamespace(execute=AsyncMock(return_value={'results':[{'url':'https://example.com/news','summary':'fresh'}]}))
        self.generator=ReplyGenerator(self.model,self.web)
        self.current=event('查一下最新版本')
        self.guard=AsyncMock(return_value=True)
        self.record=AsyncMock()

    async def generate(self):
        return await self.generator.generate([{'role':'system','content':'rules'}],ChatService.model_task,{},self.cfg,
            scope='discord:1:2',current=self.current,still_current=self.guard,record_tool=self.record)

    async def test_model_can_choose_not_to_search_for_small_talk(self):
        self.model.complete.return_value={'role':'assistant','content':'咋啦','tool_calls':[]}
        self.assertEqual(await self.generate(),'咋啦')
        self.web.execute.assert_not_awaited()

    async def test_model_chooses_search_and_reads_a_returned_page_before_answering(self):
        self.model.complete.side_effect=[tool('web_search',{'query':'latest release'}),
            tool('read_webpage',{'url':'https://example.com/news'},'call-2'),
            {'role':'assistant','content':'新版本发布了 [来源](https://example.com/news)','tool_calls':[]}]
        self.web.execute.side_effect=[{'results':[{'url':'https://example.com/news','summary':'fresh'}]},
                                     {'url':'https://example.com/news','content':'release details'}]
        self.assertIn('来源',await self.generate())
        self.assertEqual(self.web.execute.await_count,2)
        messages=self.model.complete.await_args.args[1]
        self.assertEqual(sum(message['role']=='tool' for message in messages),2)
        self.assertEqual(self.model.complete.await_args.kwargs['tool_choice'],'none')

    async def test_failed_search_is_reported_to_model_without_fabricating_evidence(self):
        self.model.complete.side_effect=[tool('web_search',{'query':'latest release'}),
            {'role':'assistant','content':'这次没查到，暂时无法核实。','tool_calls':[]}]
        self.web.execute.side_effect=ModelUnavailable('offline')
        self.assertIn('无法核实',await self.generate())
        messages=self.model.complete.await_args.args[1]
        self.assertIn('No new evidence',messages[-1]['content'])

    async def test_arbitrary_functions_and_unprovided_urls_never_execute(self):
        self.model.complete.side_effect=[tool('read_webpage',{'url':'https://unknown.example/a'}),
            {'role':'assistant','content':'请提供要看的链接。','tool_calls':[]}]
        await self.generate()
        self.web.execute.assert_not_awaited()

    async def test_context_changes_cancel_before_web_execution(self):
        self.model.complete.return_value=tool('web_search',{'query':'latest release'})
        self.guard.side_effect=[True,False]
        self.assertIsNone(await self.generate())
        self.web.execute.assert_not_awaited()

    async def test_unknown_function_is_never_forwarded_to_a_tool(self):
        self.model.complete.side_effect=[tool('run_shell',{'command':'not executed'}),
            {'role':'assistant','content':'只支持查资料。','tool_calls':[]}]
        await self.generate()
        self.web.execute.assert_not_awaited()

    async def test_tool_limit_forces_final_answer(self):
        self.cfg['web_max_calls']=1
        self.model.complete.side_effect=[tool('web_search',{'query':'latest release'}),
            {'role':'assistant','content':'查到了。','tool_calls':[]}]
        await self.generate()
        self.assertEqual(self.web.execute.await_count,1)
        self.assertEqual(self.model.complete.await_args.kwargs['tool_choice'],'none')


class ToolTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.requests=[]
        async def completion(request):
            body=await request.json(); self.requests.append(body)
            return web.json_response({'choices':[{'message':tool('web_search',{'query':'news'})}],'usage':{'prompt_tokens':10,'completion_tokens':5}})
        async def search(request):
            self.requests.append(await request.json())
            payload=json.dumps({'results':[{'url':'https://example.com','summary':'分段返回的中文资料'}]},ensure_ascii=False).encode()
            stream=web.StreamResponse(headers={'Content-Type':'application/json'})
            await stream.prepare(request)
            await stream.write(payload[:19])
            await asyncio.sleep(0.01)
            await stream.write(payload[19:])
            await stream.write_eof()
            return stream
        app=web.Application(); app.router.add_post('/chat/completions',completion); app.router.add_post('/v1/tools/search',search)
        server=TestServer(app); await server.start_server(); self.addAsyncCleanup(server.close)
        self.client=DeepSeekClient(Path(self.tmp.name),key='offline-test-key',base_url=str(server.make_url('/')).rstrip('/'))
        self.addAsyncCleanup(self.client.close)

    async def test_tool_calls_are_preserved_and_web_usage_is_durably_accounted(self):
        from bot_app.features.chat.reply import TOOLS
        message=await self.client.complete('chat_reply',[],scope='discord:1:2',tools=TOOLS)
        self.assertEqual(message['tool_calls'][0]['function']['name'],'web_search')
        self.assertEqual(self.requests[0]['thinking'],{'type':'disabled'})
        self.assertEqual(self.requests[0]['tool_choice'],'auto')
        await self.client.web_request('search',{'query':'news'},scope='discord:1:2',daily_limit=1)
        with self.assertRaises(ModelUnavailable):
            await self.client.web_request('search',{'query':'more'},scope='discord:1:2',daily_limit=1)
        usage=await self.client.usage('discord:1:')
        self.assertEqual({row['stage'] for row in usage},{'chat_reply','web_search'})

    async def test_chunked_search_response_is_fully_read_before_json_parsing(self):
        result=await self.client.web_request('search',{'query':'中文资料'},scope='discord:1:2')
        self.assertEqual(result['results'][0]['summary'],'分段返回的中文资料')


class ActiveSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_nudge_reaches_mock_send_without_a_judge(self):
        with tempfile.TemporaryDirectory() as scratch:
            model=SimpleNamespace(complete=AsyncMock(return_value='在，接着说。'))
            cfg=dict(DEFAULTS,enabled=True,channels=['2'],mode='participate',participation_style='active')
            memory=MemoryService(MemoryRepository(Path(scratch)/'memory.db'),model,
                lambda _:dict(MEMORY_DEFAULTS,enabled=True),lambda _:True)
            chat=ChatService(ChatRepository(Path(scratch)/'chat.db'),model,memory,lambda _:cfg)
            state=ChannelState(sequence=1,last_sent=time.monotonic()-1320,last_author='3')
            state.messages.extend([MessageEvent(Scope('1','2'),'99','99','bot','名字的梗？',time.time()-1320,is_bot=True),
                                   event('回一下？')])
            state.can_send=AsyncMock(return_value=True)
            state.send=AsyncMock(return_value=MessageEvent(Scope('1','2'),'200','99','bot','在，接着说。',time.time(),is_bot=True))
            try:
                await chat.round('discord:1:2',state,1)
                model.complete.assert_awaited_once()
                self.assertEqual(model.complete.await_args.args[0],'chat_reply')
                state.send.assert_awaited_once_with('在，接着说。','100')
            finally:
                await chat.close(); await memory.close()

    async def test_malformed_judge_has_a_specific_safe_error_without_sending(self):
        with tempfile.TemporaryDirectory() as scratch:
            model=SimpleNamespace(complete=AsyncMock(return_value={'a':'r','r':'useful','i':999}))
            cfg=dict(DEFAULTS,enabled=True,channels=['2'],mode='participate',participation_style='active')
            memory=MemoryService(MemoryRepository(Path(scratch)/'memory.db'),model,lambda _:dict(MEMORY_DEFAULTS),lambda _:True)
            chat=ChatService(ChatRepository(Path(scratch)/'chat.db'),model,memory,lambda _:cfg)
            state=ChannelState(sequence=1); state.messages.append(event())
            state.can_send=AsyncMock(return_value=True); state.send=AsyncMock()
            try:
                await chat.round('discord:1:2',state,1)
                row=(await chat.call('recent','discord:1:2'))[0]
                self.assertEqual(row['reason'],'judge_invalid_index')
                self.assertEqual(json.loads(row['detail'])['visible_indexes'],[0])
                self.assertNotIn(state.messages[-1].content,row['detail'])
                self.assertEqual(model.complete.await_count,2)
                state.send.assert_not_awaited()
            finally:
                await chat.close();await memory.close()

    async def test_one_bounded_judge_repair_can_recover_to_a_single_send(self):
        with tempfile.TemporaryDirectory() as scratch:
            model=SimpleNamespace(complete=AsyncMock(side_effect=[{'a':'r','i':99},
                {'a':'r','r':'useful','i':0},'这游戏的配乐不错。']))
            cfg=dict(DEFAULTS,enabled=True,channels=['2'],mode='participate',participation_style='active')
            memory=MemoryService(MemoryRepository(Path(scratch)/'memory.db'),model,lambda _:dict(MEMORY_DEFAULTS),lambda _:True)
            chat=ChatService(ChatRepository(Path(scratch)/'chat.db'),model,memory,lambda _:cfg)
            state=ChannelState(sequence=1);state.messages.append(event())
            state.can_send=AsyncMock(return_value=True)
            state.send=AsyncMock(return_value=MessageEvent(Scope('1','2'),'200','99','bot','这游戏的配乐不错。',time.time(),is_bot=True))
            try:
                await chat.round('discord:1:2',state,1)
                self.assertEqual([c.args[0] for c in model.complete.await_args_list],['chat_judge','chat_judge','chat_reply'])
                self.assertEqual(model.complete.await_args_list[1].kwargs['max_tokens'],96)
                state.send.assert_awaited_once()
            finally:
                await chat.close();await memory.close()

    async def test_restart_restores_the_recipient_of_the_last_bot_turn(self):
        with tempfile.TemporaryDirectory() as scratch:
            model=SimpleNamespace(complete=AsyncMock())
            cfg=dict(DEFAULTS,enabled=True,channels=['2'],mode='participate',participation_style='active')
            memory=MemoryService(MemoryRepository(Path(scratch)/'memory.db'),model,
                lambda _:dict(MEMORY_DEFAULTS,enabled=True),lambda _:True)
            now=time.time()
            await memory.ingest(MessageEvent(Scope('1','2'),'100','3','member','说说这个梗',now-125))
            await memory.ingest(MessageEvent(Scope('1','2'),'101','99','bot','拿我名字当梗了？',now-120,is_bot=True))
            chat=ChatService(ChatRepository(Path(scratch)/'chat.db'),model,memory,lambda _:cfg)
            chat._drain=AsyncMock()
            try:
                await chat.receive(event('有没有可能你的名字就是这么取的',identifier='102'),AsyncMock(),AsyncMock(return_value=True))
                state=chat.channels['discord:1:2']
                await state.task
                self.assertEqual(state.last_author,'3')
                self.assertTrue(participation_plan(state,cfg,time.monotonic()).followup)
                model.complete.assert_not_awaited()
            finally:
                await chat.close(); await memory.close()

    async def test_unmentioned_followup_is_delayed_then_reconsidered_without_new_input(self):
        with tempfile.TemporaryDirectory() as scratch:
            model=SimpleNamespace(complete=AsyncMock(return_value={'action':'skip','reason':'repetitive','target_id':'100'}))
            cfg=dict(DEFAULTS,enabled=True,channels=['2'],mode='participate',participation_style='active',
                     cooldown_seconds=1,debounce_seconds=0,max_wait_seconds=1)
            memory=MemoryService(MemoryRepository(Path(scratch)/'memory.db'),model,lambda _:dict(MEMORY_DEFAULTS),lambda _:True)
            chat=ChatService(ChatRepository(Path(scratch)/'chat.db'),model,memory,lambda _:cfg)
            state=ChannelState(); state.messages.append(event()); state.sequence=1
            state.last_sent=time.monotonic()-0.9; state.last_author='3'
            state.first_pending=state.last_pending=time.monotonic()
            state.can_send=AsyncMock(return_value=True); state.send=AsyncMock()
            try:
                await asyncio.wait_for(chat._drain('discord:1:2',state),2)
                model.complete.assert_awaited_once()
                state.send.assert_not_awaited()
            finally:
                await chat.close(); await memory.close()
