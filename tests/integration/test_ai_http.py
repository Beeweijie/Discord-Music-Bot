"""Local HTTP transport and dashboard integration; never calls external services."""
import copy
import sqlite3
from contextlib import closing
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

from bot_app.shared.llm.deepseek import DeepSeekClient, ModelUnavailable
from bot_app.app.configuration import JsonSettingsRepository
from bot_app.app.management import ManagementService
from bot_app.app.ai import AIRuntime
from bot_app.host.web.server import create_app
from bot_app.shared.identity import Scope, MessageEvent

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.output='hello'; self.requests=[]
        self.usage={'prompt_tokens':10,'completion_tokens':4,'prompt_cache_hit_tokens':8,'prompt_cache_miss_tokens':2}
        async def respond(request):
            self.requests.append((dict(request.headers),await request.json()))
            return web.json_response({'choices':[{'message':{'content':self.output}}], 'usage':self.usage})
        app=web.Application(); app.router.add_post('/chat/completions',respond)
        self.server=TestServer(app); await self.server.start_server(); self.addAsyncCleanup(self.server.close)
        self.client=DeepSeekClient(Path(self.tmp.name),key='offline-test-key',base_url=str(self.server.make_url('/')).rstrip('/'))
        self.addAsyncCleanup(self.client.close)

    async def test_transport_and_durable_usage_reservation(self):
        result=await self.client.complete('chat_reply',[{'role':'user','content':'hello'}],scope='discord:1:2')
        self.assertEqual(result,'hello')
        headers,payload=self.requests[0]
        self.assertEqual(headers['Authorization'],'Bearer offline-test-key')
        self.assertEqual(payload['thinking'],{'type':'disabled'})
        usage=await self.client.usage('discord:1:')
        self.assertEqual(usage[0]['input_tokens'],10)
        self.assertEqual(usage[0]['status'],'completed')
        self.assertEqual(usage[0]['cache_hit_tokens'],8)
        self.assertEqual(usage[0]['cache_miss_tokens'],2)
        self.assertEqual(usage[0]['cache_reported'],1)

    async def test_old_usage_ledger_is_migrated_without_losing_reservations(self):
        with closing(sqlite3.connect(self.client.path)) as db, db:
            db.execute('CREATE TABLE usage(id TEXT PRIMARY KEY,day TEXT,scope TEXT,stage TEXT,model TEXT,status TEXT,input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,created REAL)')
            db.execute("INSERT INTO usage VALUES('old','2026-10-01','discord:1:2','chat_reply','test','completed',20,4,0)")
        await self.client.complete('chat_reply',[],scope='discord:1:2')
        rows = await self.client.usage('discord:1:')
        self.assertEqual(sum(row['requests'] for row in rows),2)
        self.assertEqual(sum(row['cache_reported'] for row in rows),1)

    async def test_missing_or_invalid_cache_metrics_are_unknown_not_fabricated(self):
        for usage in [None, {'prompt_tokens':10,'prompt_cache_hit_tokens':999,'prompt_cache_miss_tokens':-989}]:
            self.usage=usage
            self.assertEqual(await self.client.complete('chat_reply',[],scope='discord:1:2'),'hello')
        rows=await self.client.usage('discord:1:')
        self.assertEqual(sum(row['cache_reported'] for row in rows),0)

    async def test_judge_and_reply_share_a_durable_daily_budget(self):
        await self.client.complete('chat_judge',[],scope='discord:1:2',daily_limit=1)
        with self.assertRaises(ModelUnavailable):
            await self.client.complete('chat_reply',[],scope='discord:1:2',daily_limit=1)
        self.assertEqual(len(self.requests),1)

    async def test_json_empty_and_non_object_results_fail_closed(self):
        for content in ['', '[1,2]', 'invalid json']:
            self.output=content
            with self.assertRaises(ModelUnavailable):
                await self.client.complete('memory_extract',[],scope='discord:1:2',json_mode=True)
        with self.assertRaises(ModelUnavailable):
            await self.client.complete('memory_extract',[],scope='discord:1:2',json_mode=True)
        self.assertEqual(len(self.requests),3)

class DashboardAITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.management=ManagementService(Mock(),JsonSettingsRepository(root/'config'),Mock())
        self.management.runtime.read_status.return_value = {}
        self.management.catalog=lambda:[{'id':'1','name':'test','channels':[{'id':'2','name':'chat'}]}]
        self.runtime=AIRuntime(self.management,root/'data'); self.runtime.model.key=''
        self.management.ai=self.runtime
        self.addAsyncCleanup(self.runtime.close)
        self.client=TestClient(TestServer(create_app(self.management)))
        await self.client.start_server(); self.addAsyncCleanup(self.client.close)
        self.headers={'X-Requested-With':'BotDashboard'}
        self.scope=Scope('1','2')
        message=MessageEvent(self.scope,'100','3','Alice','I like jazz',time.time())
        await self.runtime.memory.call('ingest',message)
        await self.runtime.memory.call('queue',self.scope.key,batch_size=1,wait_seconds=10,force=True)
        job=await self.runtime.memory.call('claim')
        ids=await self.runtime.memory.call('apply',job,[{'kind':'fact','subject_id':'3','content':'Likes jazz',
            'source_ids':['100'],'attribution':'self_report','slot':'music'}])
        self.memory_id=ids[0]

    async def test_dashboard_assets_and_scoped_memory_are_available(self):
        for path in ['/','/app.js','/ai.js','/ai.css','/memory.js']:
            response=await self.client.get(path)
            self.assertEqual(response.status,200,path)
        response=await self.client.get('/api/v1/memory?guild_id=1&channel_id=2')
        self.assertEqual((await response.json())['memories'][0]['subject'],'3')

    async def test_cross_server_channel_and_pagination_are_rejected(self):
        for query in ['guild_id=9&channel_id=2','guild_id=1&channel_id=9','guild_id=1&channel_id=2&offset=-1']:
            self.assertEqual((await self.client.get('/api/v1/memory?'+query)).status,400)

    async def test_memory_writes_require_dashboard_origin_guard(self):
        path=f'/api/v1/memory/{self.memory_id}/correct'
        body={'guild_id':'1','channel_id':'2','text':'Prefers classical'}
        self.assertEqual((await self.client.post(path,json=body)).status,403)
        response=await self.client.post(path,json=body,headers=self.headers)
        self.assertEqual(response.status,200)
        rows=await self.runtime.memory.list(self.scope.key,'3')
        self.assertEqual(rows[0]['content'],'Prefers classical')
        response=await self.client.get('/api/v1/memory?guild_id=1&channel_id=2&history=1')
        history=(await response.json())['memories']
        self.assertEqual({row['state'] for row in history},{'active','superseded'})

    async def test_forget_removes_source_and_derived_records(self):
        response=await self.client.post('/api/v1/memory/forget',headers=self.headers,
            json={'guild_id':'1','channel_id':'2','subject_id':'3'})
        self.assertEqual(response.status,200)
        self.assertFalse(await self.runtime.memory.list(self.scope.key,'3'))

    async def test_chat_settings_are_inherited_and_report_missing_key(self):
        cfg=self.management.get_settings('chat','1')
        cfg.update(enabled=True,channels=['2'])
        response=await self.client.put('/api/v1/settings/chat?guild_id=1',headers=self.headers,json=cfg)
        self.assertEqual(response.status,200)
        self.assertTrue(self.management.get_settings('chat','1')['enabled'])
        response=await self.client.get('/api/v1/ai/status?guild_id=1')
        self.assertEqual((await response.json())['state'],'missing_api_key')
