"""Pinned Git source updates and durable AI data preservation; no external requests."""
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot_app.features.updates.engine import GitHubUpdater, REQUIRED, atomic_json, extract_source, read_json
from bot_app.features.updates.policies import UpdateError, update_target
from bot_app.features.memory.storage import MemoryRepository
from bot_app.shared.identity import Scope, MessageEvent

REPO = 'Beeweijie/Discord-Music-Bot'
SHA = 'a1b2c3d4' * 5
TREE = 'c' * 40
VERSION = 'v1.4.0'


def source_bundle(extra=None, schema=1):
    content = {name:b'# candidate\n' for name in REQUIRED}
    content.update({'requirements.txt':b'', 'bot_app/features/memory/storage.py':b'# new memory implementation\n',
                    '.env':b'not-a-real-credential', 'config/music.json':b'{"overwrite":true}',
                    'data/memory/memory.sqlite3':b'never install repository data',
                    '.github/workflows/test.yml':b'name: ignored', 'assets/example.wav':b'ignored'})
    if extra:
        content.update(extra)
    content['version.json'] = json.dumps({'version':VERSION, 'update_schema':1,
                                         'files':[], 'data_schemas':{'memory':schema}}).encode()
    tree = {'sha':TREE, 'truncated':False, 'tree':[
        {'path':name,'type':'blob','mode':'100644',
         'sha':hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()}
        for name,data in content.items()]}
    output = io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('repository-root/',b'')
        for name,data in content.items():
            archive.writestr('repository-root/' + name,data)
    return output.getvalue(), tree


class TargetTests(unittest.TestCase):
    def test_release_branch_and_commit_are_unambiguous(self):
        self.assertEqual(update_target('',REPO),('release',''))
        self.assertEqual(update_target('https://github.com/' + REPO,REPO),('release',''))
        self.assertEqual(update_target('branch:feature/chatbot',REPO),('branch','feature/chatbot'))
        for text in [SHA[:8], 'commit:' + SHA.upper()]:
            self.assertEqual(update_target(text,REPO)[0],'commit')
        for text in ['chatbot','a1b2','branch:../main','branch:feature//chat','branch:HEAD',
                     'branch:main.lock','branch:main?x=1','branch:main@{1}',
                     'https://github.com/someone/another-repository']:
            with self.subTest(target=text), self.assertRaises(UpdateError):
                update_target(text,REPO)


class SourceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.updater = GitHubUpdater(self.root)
        atomic_json(self.root/'version.json',{'version':VERSION,'files':['main.py','bot_app/obsolete.py']})
        for name,data in {'main.py':b'# original launcher', 'bot_app/obsolete.py':b'# old module',
                          'bot_app/features/memory/storage.py':b'# original memory implementation',
                          '.env':b'DISCORD_TOKEN=local-placeholder',
                          'config/guilds/1/chat.json':b'{"enabled":true}',
                          'config/music.json':b'{"local":true}'}.items():
            path = self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
        self.memory = MemoryRepository(self.root/'data/memory/memory.sqlite3')
        message = MessageEvent(Scope('1','2'),'100','3','Member','I like jazz',1_793_000_000)
        self.memory.ingest(message)
        self.memory.queue(message.scope.key,batch_size=1,wait_seconds=10,force=True)
        self.memory.apply(self.memory.claim(),[{'kind':'fact','subject_id':'3','content':'Likes jazz',
            'source_ids':['100'],'slot':'music','attribution':'self_report'}])
        self.original = {name:(self.root/name).read_bytes() for name in
                         ['.env','config/music.json','config/guilds/1/chat.json','data/memory/memory.sqlite3']}

    def prepare(self, target='branch:chatbot', schema=1, tree_edit=None, archive_edit=None):
        payload,tree = source_bundle(schema=schema)
        if tree_edit:
            tree_edit(tree)
        if archive_edit:
            payload = archive_edit(payload)
        commit = {'sha':SHA, 'commit':{'tree':{'sha':TREE}}}
        def environment(job):
            path = job/'environment'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            path.parent.mkdir(parents=True); path.write_bytes(b'fake-python'); return path
        first = {'commit':commit} if target.startswith('branch:') else [commit]
        with patch('bot_app.features.updates.engine.download',side_effect=[json.dumps(first).encode(),json.dumps(tree).encode(),payload]) as download, \
             patch.object(self.updater,'prepare_environment',side_effect=environment) as install:
            self.download,self.install = download,install
            return self.updater.prepare(target)

    def assert_preserved(self):
        for name,content in self.original.items():
            self.assertEqual((self.root/name).read_bytes(),content,name)

    def test_branch_is_pinned_and_only_application_blobs_are_staged(self):
        ticket = self.prepare('branch:feature/chatbot')
        urls = [call.args[0] for call in self.download.call_args_list]
        self.assertIn('/branches/feature%2Fchatbot',urls[0])
        self.assertEqual(urls[-1],f'https://codeload.github.com/{REPO}/zip/{SHA}')
        job = self.updater.job(ticket['id'])
        metadata = read_json(job/'source/version.json')
        self.assertEqual(metadata['source']['commit'],SHA)
        self.assertIn(SHA[:12],ticket['label'])
        self.assertIn('bot_app/features/memory/storage.py',metadata['files'])
        for name in ['.env','config/music.json','data/memory/memory.sqlite3','.github/workflows/test.yml']:
            self.assertNotIn(name,metadata['files'])
            self.assertFalse((job/'source'/name).exists())
        self.assert_preserved()

    def test_short_commit_resolves_to_a_full_immutable_commit(self):
        ticket = self.prepare(SHA[:8])
        self.assertIn('/commits?sha=' + SHA[:8] + '&per_page=1',self.download.call_args_list[0].args[0])
        plan = read_json(self.updater.job(ticket['id'])/'plan.json')
        self.assertEqual(plan['source']['kind'],'commit')
        self.assertEqual(plan['source']['commit'],SHA)

    def test_same_commit_does_not_redownload_or_prepare_environment(self):
        atomic_json(self.root/'version.json',{'version':VERSION,'source':{'commit':SHA,'repository':REPO}})
        commit = {'sha':SHA,'commit':{'tree':{'sha':TREE}}}
        with patch('bot_app.features.updates.engine.download',return_value=json.dumps({'commit':commit}).encode()) as download, \
             patch.object(self.updater,'prepare_environment') as install:
            self.assertIsNone(self.updater.prepare('branch:chatbot'))
        self.assertEqual(download.call_count,1); install.assert_not_called()

    def test_resolution_must_match_the_requested_short_sha(self):
        commit = {'sha':'b'*40,'commit':{'tree':{'sha':TREE}}}
        with patch('bot_app.features.updates.engine.download',return_value=json.dumps([commit]).encode()) as download:
            with self.assertRaisesRegex(UpdateError,'immutable'):
                self.updater.prepare(SHA[:8])
        self.assertEqual(download.call_count,1)

    def test_incomplete_tree_and_symlink_blobs_are_rejected(self):
        for edit in [lambda tree:tree.update(truncated=True),
                     lambda tree:next(row for row in tree['tree'] if row['path']=='main.py').update(mode='120000')]:
            with self.subTest(edit=edit), self.assertRaises(UpdateError):
                self.prepare(tree_edit=edit)
            self.install.assert_not_called()
        self.assert_preserved()

    def test_archive_tampering_and_traversal_fail_before_environment_preparation(self):
        def changed(payload, name, data, symlink=False):
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(payload)) as original, zipfile.ZipFile(output,'w') as edited:
                for info in original.infolist():
                    if info.filename != name:
                        edited.writestr(info,original.read(info))
                info = zipfile.ZipInfo(name)
                if symlink:
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                edited.writestr(info,data)
            return output.getvalue()
        for name,link in [('repository-root/main.py',False),('repository-root/../escape.py',False),
                          ('repository-root/bot_app/link.py',True),('repository-root/MAIN.PY',False)]:
            with self.subTest(name=name), self.assertRaises(UpdateError):
                self.prepare(archive_edit=lambda data:changed(data,name,b'tampered',link))
            self.install.assert_not_called()
        self.assert_preserved()

    def test_incompatible_memory_schema_is_rejected_before_installing_dependencies(self):
        with self.assertRaisesRegex(UpdateError,'incompatible'):
            self.prepare(schema=2)
        self.install.assert_not_called()
        self.assert_preserved()

    def test_apply_updates_memory_code_but_preserves_and_backs_up_all_memory(self):
        ticket = self.prepare()
        self.updater.activate(ticket,123)
        request = read_json(self.updater.pending)
        with patch('bot_app.features.updates.engine.subprocess.run',return_value=SimpleNamespace(returncode=0)):
            self.updater.apply(request)
        self.assertEqual((self.root/'bot_app/features/memory/storage.py').read_bytes(),b'# new memory implementation\n')
        self.assertEqual(read_json(self.root/'version.json')['source']['commit'],SHA)
        self.assert_preserved()
        backup = self.updater.job(ticket['id'])/'data-backup/data/memory/memory.sqlite3'
        self.assertEqual(MemoryRepository(backup).list('discord:1:2')[0]['content'],'Likes jazz')
        self.updater.rollback(request)
        self.assertEqual((self.root/'main.py').read_bytes(),b'# original launcher')
        self.assertFalse(self.updater.active.exists())
        self.assert_preserved()

    def test_schema_changed_after_preparation_prevents_code_replacement(self):
        ticket = self.prepare()
        self.updater.activate(ticket,123)
        with closing(sqlite3.connect(self.memory.path)) as db,db:
            db.execute('UPDATE meta SET version=2')
        with self.assertRaisesRegex(UpdateError,'incompatible'):
            self.updater.apply(read_json(self.updater.pending))
        self.assertEqual((self.root/'main.py').read_bytes(),b'# original launcher')

    def test_failed_database_backup_does_not_replace_code(self):
        ticket = self.prepare()
        self.updater.activate(ticket,123)
        path = self.root/'data/chat/chat.sqlite3'; path.parent.mkdir(parents=True); path.write_bytes(b'invalid database')
        with self.assertRaisesRegex(UpdateError,'back up'):
            self.updater.apply(read_json(self.updater.pending))
        self.assertEqual((self.root/'main.py').read_bytes(),b'# original launcher')
        self.assert_preserved()

    def test_ready_marker_checks_commit_even_when_release_version_matches(self):
        ticket = self.prepare()
        job = self.updater.job(ticket['id'])
        self.updater.mark_ready(ticket['id'])
        self.assertFalse((job/'ready.json').exists())
        metadata = read_json(job/'source/version.json')
        atomic_json(self.root/'version.json',metadata)
        self.updater.mark_ready(ticket['id'])
        self.assertTrue((job/'ready.json').exists())


class SourceCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_command_passes_branch_and_commit_targets(self):
        from bot_app.host.discord.bot import create_bot
        bot = create_bot()
        bot.close = AsyncMock()
        bot.updates.prepare = AsyncMock(return_value=None)
        ctx = SimpleNamespace(send=AsyncMock())
        with patch.dict(os.environ,{'BOT_SUPERVISED':'1'}):
            for target in ['branch:chatbot',SHA[:8]]:
                await bot.get_command('update').callback(ctx,target)
                bot.updates.prepare.assert_awaited_with(target)
        bot.close.assert_not_awaited()
