"""Offline end-to-end AI feature tests; no credentials, Discord login or network."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot_app.shared.identity import Scope, MessageEvent
from bot_app.features.memory.storage import MemoryRepository
from bot_app.features.memory.api import MemoryService
from bot_app.features.chat.storage import ChatRepository
from bot_app.features.chat.service import ChatService, ChannelState
from bot_app.features.chat.settings import DEFAULTS as CHAT_DEFAULTS, validate
from bot_app.features.memory.settings import DEFAULTS as MEMORY_DEFAULTS
from bot_app.app.configuration import JsonSettingsRepository

def event(identifier='100', author='3', text='我喜欢摇滚音乐', channel='2', direct=False, bot=False, bot_user_id='99'):
    return MessageEvent(Scope('1', channel), identifier, author, 'member-' + author, text, time.time(), direct=direct, is_bot=bot, bot_user_id=bot_user_id)

def fact(message, text='member likes rock music', **kwargs):
    return dict(kind='fact', subject_id=message.author_id, content=text, source_ids=[message.message_id],
                attribution='self_report', slot='music_preference', **kwargs)

class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = MemoryRepository(Path(self.tmp.name) / 'memory.db')
        self.scope = Scope('1', '2').key

    def ingest_job(self, message):
        self.repo.ingest(message)
        self.repo.queue(message.scope.key, batch_size=1, wait_seconds=10, force=True)
        return self.repo.claim()

    def test_fact_evidence_and_persistence_across_restart(self):
        message = event()
        created = self.repo.apply(self.ingest_job(message), [fact(message)])
        restarted = MemoryRepository(self.repo.path)
        rows = restarted.list(self.scope)
        self.assertEqual(rows[0]['id'], created[0])
        self.assertEqual(rows[0]['sources'][0]['author'], '3')
        self.assertEqual(restarted.search(self.scope, 'rock')[0]['id'], created[0])

    def test_channel_and_member_identity_are_not_names(self):
        a, b = event(), event('101', '4', channel='5')
        self.repo.apply(self.ingest_job(a), [fact(a)])
        self.repo.apply(self.ingest_job(b), [fact(b, 'other private fact')])
        self.assertEqual(len(self.repo.list(self.scope)), 1)
        self.assertEqual(self.repo.list(self.scope, subject='4'), [])
        self.assertEqual(self.repo.search(self.scope, 'other private'), [])

    def test_forged_subject_or_evidence_is_rejected(self):
        message = event()
        forged = fact(message); forged['subject_id'] = '99'
        invalid = fact(message); invalid['source_ids'] = ['888']
        self.repo.apply(self.ingest_job(message), [forged, invalid])
        self.assertEqual(self.repo.list(self.scope), [])

    def test_observation_is_a_candidate_until_approved(self):
        message = event()
        self.repo.ingest(message, observed=True)
        self.repo.queue(self.scope, batch_size=1, wait_seconds=10, force=True)
        ids = self.repo.apply(self.repo.claim(), [fact(message)])
        self.assertFalse(self.repo.list(self.scope))
        self.assertEqual(self.repo.list(self.scope, candidates=True)[0]['state'], 'candidate')
        self.repo.approve(self.scope, ids[0])
        self.assertEqual(len(self.repo.list(self.scope)), 1)

    def test_edit_invalidates_old_fact_and_stale_extraction(self):
        first = event()
        job = self.ingest_job(first)
        self.repo.apply(job, [fact(first)])
        self.repo.ingest(event(text='我其实喜欢古典'))
        self.assertFalse(self.repo.list(self.scope))
        self.assertFalse(self.repo.apply(job, [fact(first)]))

    def test_delete_and_forget_cannot_be_resurrected_by_pending_jobs(self):
        message = event()
        job = self.ingest_job(message)
        self.repo.forget(self.scope, actor='3')
        self.assertFalse(self.repo.apply(job, [fact(message)]))
        self.assertFalse(self.repo.ingest(message))
        self.assertEqual(self.repo.recent(self.scope), [])

    def test_self_correction_preserves_history_and_blocks_other_members(self):
        message = event()
        original = self.repo.apply(self.ingest_job(message), [fact(message)])[0]
        with self.assertRaises(ValueError):
            self.repo.correct(self.scope, original, 'forged correction', actor='4')
        corrected = self.repo.correct(self.scope, original, 'prefers classical', actor='3')
        rows = self.repo.list(self.scope)
        self.assertEqual([row['id'] for row in rows], [corrected])
        self.assertEqual(rows[0]['supersedes'], original)
        with self.assertRaises(ValueError):
            self.repo.forget(self.scope, memory_id=corrected, actor='4')

    def test_multiple_preferences_coexist_and_explicit_correction_supersedes(self):
        first, second = event(), event('101', text='也喜欢古典')
        self.repo.apply(self.ingest_job(first), [fact(first)])
        self.repo.apply(self.ingest_job(second), [fact(second, 'also likes classical')])
        self.assertEqual(len(self.repo.list(self.scope)), 2)
        third = event('102', text='更正，只喜欢电子乐')
        self.repo.apply(self.ingest_job(third), [fact(third, 'only likes electronic music', correction=True)])
        self.assertEqual(len(self.repo.list(self.scope)), 1)

    def test_retention_is_scoped_and_keeps_fact_provenance(self):
        a = event(); b = event('101', channel='5')
        self.repo.apply(self.ingest_job(a), [fact(a)])
        self.repo.apply(self.ingest_job(b), [fact(b)])
        with self.repo.db() as db:
            db.execute('UPDATE messages SET created=?', (time.time() - 40*86400,))
        self.repo.cleanup(self.scope, 30)
        self.assertEqual(self.repo.list(self.scope)[0]['sources'][0]['content'], '')
        self.assertTrue(self.repo.list(b.scope.key)[0]['sources'][0]['content'])

    def test_semantic_vectors_are_scoped_and_model_dimension_checked(self):
        message = event()
        memory_id = self.repo.apply(self.ingest_job(message), [fact(message, '喜欢听音乐')])[0]
        self.repo.vector(memory_id, ('model-a', [1.0, 0.0]))
        self.assertEqual(self.repo.search(self.scope, 'unrelated', embedding=('model-a',[1.0,0.0]))[0]['id'], memory_id)
        self.assertEqual(self.repo.search(self.scope, 'unrelated', embedding=('model-b',[1.0,0.0])), [])
        self.assertEqual(self.repo.search(Scope('1','9').key, 'unrelated', embedding=('model-a',[1.0,0.0])), [])

    def test_secret_corrections_and_empty_corrections_are_rejected(self):
        message = event()
        memory_id = self.repo.apply(self.ingest_job(message), [fact(message)])[0]
        for text in ['', 'DEEPSEEK_API_KEY=not-a-real-key']:
            with self.assertRaises(ValueError):
                self.repo.correct(self.scope, memory_id, text, actor='3')

    def test_forget_redacts_persisted_jobs_and_prevents_retry_resurrection(self):
        message = event(text='private source to remove')
        job = self.ingest_job(message)
        self.repo.forget(self.scope, actor='3')
        self.repo.fail(job, 'late model failure')
        with self.repo.db() as db:
            row = db.execute('SELECT state,payload FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual((row['state'], row['payload']), ('obsolete', '[]'))
        self.assertIsNone(self.repo.claim())

    def test_delete_before_ingestion_cannot_restore_a_source(self):
        message = event()
        self.repo.delete_message(self.scope, message.message_id)
        self.assertFalse(self.repo.ingest(message))

    def test_event_rescheduling_supersedes_event_and_old_summary(self):
        first = event(text='Friday game night')
        original = self.repo.apply(self.ingest_job(first), [dict(fact(first), kind='episode', subject_id='', slot='game_night')], 'Friday game night')
        second = event('101', text='Move game night to Saturday')
        self.repo.apply(self.ingest_job(second), [dict(fact(second, 'Saturday game night'), kind='episode', subject_id='', slot='game_night', correction=True)])
        rows = self.repo.list(self.scope)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['supersedes'], original[0])
        self.assertEqual(rows[0]['content'], 'Saturday game night')

    def test_retention_clears_unprocessed_sources_and_pending_job_payload(self):
        message = event()
        job = self.ingest_job(message)
        with self.repo.db() as db:
            db.execute('UPDATE messages SET created=?', (time.time() - 40 * 86400,))
        self.repo.cleanup(self.scope, 30)
        self.assertFalse(self.repo.apply(job, [fact(message)]))
        self.assertFalse(self.repo.recent(self.scope))
        with self.repo.db() as db:
            self.assertEqual(db.execute('SELECT payload FROM jobs').fetchone()[0], '[]')

class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(CHAT_DEFAULTS)
        self.cfg.update(enabled=True, channels=['2'], mode='participate', cooldown_seconds=0)
        self.mem_cfg = dict(MEMORY_DEFAULTS, enabled=True)
        self.model = SimpleNamespace(complete=AsyncMock(), embed=AsyncMock(return_value=None))
        self.memory = MemoryService(MemoryRepository(Path(self.tmp.name)/'memory.db'), self.model,
                                    lambda _: self.mem_cfg, lambda scope: scope == 'discord:1:2')
        self.chat = ChatService(ChatRepository(Path(self.tmp.name)/'chat.db'), self.model, self.memory, lambda _: self.cfg)
        self.addAsyncCleanup(self.memory.close); self.addAsyncCleanup(self.chat.close)
        self.scope = Scope('1','2').key
        self.state = ChannelState()
        self.state.messages.append(event(direct=True))
        self.state.sequence = 1
        self.state.can_send = AsyncMock(return_value=True)
        self.state.send = AsyncMock(return_value=event('200','99','hello',bot=True))

    async def test_direct_reply_uses_identity_and_persists_success(self):
        self.model.complete.return_value = 'hello'
        await self.chat.round(self.scope, self.state, 1)
        self.state.send.assert_awaited_once_with('hello','100')
        self.assertEqual(self.state.messages[-1].author_id,'99')

    async def test_mentioning_another_bot_only_never_calls_model_or_sends(self):
        self.state.messages.clear(); self.state.messages.append(event(text='<@1366796071304564736>'))
        await self.chat.round(self.scope,self.state,1)
        self.model.complete.assert_not_awaited()
        self.state.send.assert_not_awaited()
        self.assertEqual((await self.chat.call('recent',self.scope))[0]['reason'],'addressed_to_other')

    async def test_mentioning_self_still_uses_direct_reply_path(self):
        self.state.messages.clear(); self.state.messages.append(event(text='<@!99>',direct=True))
        self.model.complete.return_value='在呢'
        await self.chat.round(self.scope,self.state,1)
        self.model.complete.assert_awaited_once()
        self.state.send.assert_awaited_once_with('在呢','100')

    async def test_judge_cannot_claim_a_message_to_someone_else_addresses_self(self):
        self.state.messages.clear(); self.state.messages.append(event(text='<@88> 你怎么看？'))
        self.model.complete.return_value={'action':'reply','reason':'addressed','target_id':'100'}
        await self.chat.round(self.scope,self.state,1)
        self.model.complete.assert_awaited_once()
        self.state.send.assert_not_awaited()

    def test_context_exposes_actual_mention_ids_and_authenticated_bot_identity(self):
        self.state.messages.clear(); self.state.messages.append(event(text='<@88> <@!77> <@&66>'))
        prefix=self.chat.model_prefix(self.scope,self.state,self.cfg)
        self.assertIn('"bot_user_id": "99"',prefix[0]['content'])
        self.assertEqual(json.loads(prefix[1]['content'])['mentioned_user_ids'],['88','77'])

    def test_persona_and_style_examples_do_not_become_channel_history(self):
        core = '你是deepseek，也是蓝色大肥鱼\n你现在是一个喜欢在网上冲浪的群友，你爱怎么说话就怎么说话'
        self.cfg['persona'] = core
        original = [row.context() for row in self.state.messages]
        prefix = self.chat.model_prefix(self.scope,self.state,self.cfg)
        identity = json.loads(prefix[0]['content'].split('\nIdentity JSON:\n',1)[1])
        self.assertEqual(identity['persona'],core)
        self.assertEqual(self.cfg['persona'],core)
        self.assertEqual([json.loads(row['content']) for row in prefix[1:]],original)
        self.assertEqual([row.context() for row in self.state.messages],original)

    async def test_observe_mode_calls_judge_but_never_sends(self):
        self.cfg['mode'] = 'observe'
        self.state.messages.clear(); self.state.messages.append(event())
        self.model.complete.return_value = {'action':'reply','reason':'useful','target_id':'100'}
        await self.chat.round(self.scope,self.state,1)
        self.assertEqual(self.model.complete.await_count,1)
        self.state.send.assert_not_awaited()

    async def test_skip_and_wait_are_valid_silent_decisions(self):
        self.state.messages.clear(); self.state.messages.append(event())
        for action in ['skip','wait']:
            self.model.complete.return_value={'action':action,'reason':'unrelated','target_id':'100'}
            await self.chat.round(self.scope,self.state,1)
        self.state.send.assert_not_awaited()

    async def test_model_cannot_select_out_of_scope_target(self):
        self.state.messages.clear(); self.state.messages.append(event())
        self.model.complete.return_value={'action':'reply','reason':'useful','target_id':'999'}
        with self.assertRaises(ValueError):
            await self.chat.round(self.scope,self.state,1)
        self.state.send.assert_not_awaited()

    async def test_context_change_during_generation_cancels_reply(self):
        async def generate(*args,**kwargs):
            self.state.sequence += 1
            return 'old answer'
        self.model.complete.side_effect=generate
        await self.chat.round(self.scope,self.state,1)
        self.state.send.assert_not_awaited()

    async def test_new_message_during_delivery_reservation_cancels_reply(self):
        self.model.complete.return_value = 'stale answer'
        original = self.chat.call
        async def call(method, *args, **kwargs):
            result = await original(method, *args, **kwargs)
            if method == 'finish' and args[1] == 'sending':
                self.state.sequence += 1
            return result
        self.chat.call = call
        await self.chat.round(self.scope,self.state,1)
        self.state.send.assert_not_awaited()

    async def test_memory_forgetting_during_generation_cancels_reply(self):
        await self.memory.call('epoch',self.scope)
        async def generate(*args,**kwargs):
            await self.memory.forget(self.scope,'3')
            return 'old personal information'
        self.model.complete.side_effect=generate
        await self.chat.round(self.scope,self.state,1)
        self.state.send.assert_not_awaited()

    async def test_permission_or_settings_change_cancels_reply(self):
        self.model.complete.return_value='hello'
        self.state.can_send.return_value=False
        await self.chat.round(self.scope,self.state,1)
        self.state.send.assert_not_awaited()

    async def test_failed_send_is_not_blindly_retried(self):
        self.model.complete.return_value='hello'
        self.state.send.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.chat.round(self.scope,self.state,1)
        await self.chat.round(self.scope,self.state,1)
        self.assertEqual(self.state.send.await_count,1)

    async def test_disabled_and_secret_messages_do_not_reach_model(self):
        await self.chat.receive(event(text='DEEPSEEK_API_KEY=fake'),self.state.send,self.state.can_send)
        self.cfg['enabled']=False
        await self.chat.receive(event('101'),self.state.send,self.state.can_send)
        self.assertEqual(self.chat.channels,{})
        self.model.complete.assert_not_awaited()

    async def test_deleted_message_is_removed_from_short_context(self):
        self.chat.channels[self.scope]=self.state
        await self.chat.deleted(self.scope,'100')
        self.assertEqual(len(self.state.messages),0)

    async def test_broken_memory_degrades_to_short_term_chat(self):
        self.memory.repository.path.write_bytes(b'not a sqlite database')
        self.model.complete.return_value = 'hello'
        await self.chat.round(self.scope, self.state, 1)
        self.state.send.assert_awaited_once()

    async def test_thin_judge_and_full_reply_load_different_contexts(self):
        self.state.messages.clear(); self.state.messages.append(event())
        self.model.complete.side_effect = [{'action':'reply','reason':'useful','target_id':'100'}, 'hello']
        await self.chat.round(self.scope, self.state, 1)
        judge, reply = [call.args[1] for call in self.model.complete.await_args_list]
        self.assertEqual(len(judge),2)
        self.assertIn('msgs',json.loads(judge[1]['content']))
        self.assertIn('Identity JSON',reply[0]['content'])
        self.assertNotIn('Identity JSON',judge[0]['content'])
        self.assertEqual(self.model.complete.await_args_list[0].kwargs['max_tokens'],64)
        self.assertEqual(self.model.complete.await_args_list[1].kwargs['max_tokens'],300)
        self.assertNotIn('seconds_since_bot', judge[0]['content'])
        self.assertNotIn('now', json.loads(judge[1]['content']))
        self.assertNotEqual(judge[-1], reply[-1])

    def test_history_appends_until_coarse_rotation_and_respects_limits(self):
        self.cfg['context_messages'] = 8
        self.state.messages = __import__('collections').deque(event(str(100+i)) for i in range(7))
        first = self.chat.model_prefix(self.scope, self.state, self.cfg)
        self.state.messages.append(event('107'))
        second = self.chat.model_prefix(self.scope, self.state, self.cfg)
        self.assertEqual(first, second[:-1])
        self.state.messages.append(event('108'))
        rotated = self.chat.model_prefix(self.scope, self.state, self.cfg)
        self.assertEqual(len(rotated) - 1, 6)
        self.assertEqual(json.loads(rotated[1]['content'])['message_id'], '103')

    async def test_memory_worker_extracts_sources_and_clears_finished_payload(self):
        self.mem_cfg.update(batch_messages=1)
        message = event()
        completed = asyncio.Event()
        async def extract(*args, **kwargs):
            completed.set()
            return {'items':[fact(message)], 'summary':'Likes rock music'}
        self.model.complete.side_effect = extract
        await self.memory.ingest(message)
        self.memory.start()
        await asyncio.wait_for(completed.wait(), 3)
        for _ in range(20):
            rows = await self.memory.list(self.scope)
            if rows:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(rows)
        self.assertIn('timezone', json.loads(self.model.complete.await_args.args[1][-1]['content']))
        def jobs():
            with self.memory.repository.db() as db:
                return [tuple(row) for row in db.execute('SELECT state,payload FROM jobs')]
        result = await asyncio.get_running_loop().run_in_executor(self.memory.executor, jobs)
        self.assertEqual(result, [('done','[]')])

    async def test_missing_api_key_pauses_queued_extraction_without_losing_sources(self):
        self.mem_cfg['batch_messages'] = 1
        self.model.configured = False
        await self.memory.ingest(event())
        self.memory.start()
        for _ in range(30):
            if self.memory.error == 'MissingAPIKey':
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.memory.error, 'MissingAPIKey')
        self.model.complete.assert_not_awaited()
        job = await self.memory.call('claim')
        self.assertIsNotNone(job)
        self.assertEqual(job['attempts'], 1)

    async def test_explicit_note_uses_memory_public_api_and_enforces_admission(self):
        note = event(text='Remember that I like jazz')
        self.assertTrue(await self.memory.remember(note))
        self.assertEqual((await self.memory.list(self.scope,'3'))[0]['content'],note.content)
        self.assertFalse(await self.memory.remember(event('101',channel='9')))
        self.assertFalse(await self.memory.remember(event('102',text='DEEPSEEK_API_KEY=example')))
        restored = await self.memory.recent_context(note.scope,10)
        self.assertEqual(restored[0].message_id,note.message_id)

class AIConfigurationTests(unittest.TestCase):
    def test_defaults_are_disabled_and_legacy_snapshots_stay_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository=JsonSettingsRepository(Path(tmp))
            self.assertFalse(repository.read('chat','1')['enabled'])
            self.assertFalse((Path(tmp)/'guilds/1/chat.json').exists())
            before=repository.read('music','1')
            global_music=repository.read('music'); global_music['default_volume']=22
            repository.write('music',global_music)
            self.assertEqual(repository.read('music','1'),before)

    def test_chat_overrides_are_validated_and_inherit_unmodified_fields(self):
        cfg=copy.deepcopy(CHAT_DEFAULTS)
        cfg['channel_overrides']={'2':{'mode':'participate'}}
        validate(cfg)
        cfg['channel_overrides']['2']['channels']=['9']
        with self.assertRaises(ValueError): validate(cfg)

    def test_invalid_scope_ids_are_rejected(self):
        for value in ['../x','0','１２',str(2**64)]:
            with self.assertRaises(ValueError): Scope('1',value)
