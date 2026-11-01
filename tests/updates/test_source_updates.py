"""Pinned source updates, archive trust, local state preservation and rollback."""
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot_app.features.updates.engine import (
    GitHubUpdater, REQUIRED, LEGACY_REQUIRED, atomic_json, extract_source, read_json,
)
from bot_app.features.updates.policies import UpdateError, update_target

REPO = 'Beeweijie/Discord-Music-Bot'
SHA = 'a1b2c3d4' * 5
TREE = 'c' * 40
VERSION = 'v1.3.0'
MODULE = 'bot_app/features/music/service.py'
LOCAL = {
    '.env': b'DISCORD_TOKEN=local-placeholder',
    'config/music.json': b'{"local":true}',
    'config/guilds/1/music.json': b'{"default_volume":25}',
    'data/queues/1.json': b'{"tracks":[]}',
    'data/library/1.json': b'{"favorites":[]}',
    # Retired features' data must remain untouched, even if it is not valid SQLite.
    'data/memory/memory.sqlite3': b'opaque retired data',
    'data/chat/chat.sqlite3': b'opaque retired chat data',
}


def source_bundle(required=REQUIRED, extra=None, protocol=1):
    content = {name: b'# candidate\n' for name in required}
    content.update(LOCAL)
    content.update({'requirements.txt': b'', MODULE: b'# new music implementation\n',
                    '.github/workflows/test.yml': b'name: ignored', 'assets/example.wav': b'ignored'})
    content.update(extra or {})
    content['version.json'] = json.dumps({'version': VERSION, 'update_schema': protocol, 'files': []}).encode()
    tree = {'sha': TREE, 'truncated': False, 'tree': [
        {'path': name, 'type': 'blob', 'mode': '100644',
         'sha': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()}
        for name, data in content.items()]}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('repository-root/', b'')
        for name, data in content.items():
            archive.writestr('repository-root/' + name, data)
    return output.getvalue(), tree


class TargetTests(unittest.TestCase):
    def test_release_branch_and_commit_are_unambiguous(self):
        self.assertEqual(update_target('', REPO), ('release', ''))
        self.assertEqual(update_target('https://github.com/' + REPO, REPO), ('release', ''))
        self.assertEqual(update_target('branch:codex/haiyang-voice', REPO), ('branch', 'codex/haiyang-voice'))
        for value in (SHA[:8], 'commit:' + SHA.upper()):
            self.assertEqual(update_target(value, REPO)[0], 'commit')
        for value in ('main', 'a1b2', 'branch:../main', 'branch:feature//voice', 'branch:HEAD',
                      'branch:main.lock', 'branch:main?x=1', 'branch:main@{1}',
                      'https://github.com/someone/another-repository'):
            with self.subTest(value=value), self.assertRaises(UpdateError):
                update_target(value, REPO)


class SourceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.updater = GitHubUpdater(self.root)
        atomic_json(self.root / 'version.json', {'version': VERSION, 'files': ['main.py', 'bot_app/obsolete.py']})
        for name, data in dict(LOCAL, **{'main.py': b'# original launcher',
                                        'bot_app/obsolete.py': b'# obsolete', MODULE: b'# original music'}).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def prepare(self, target='branch:main', tree_edit=None, archive_edit=None, required=REQUIRED, extra=None, protocol=1):
        payload, tree = source_bundle(required, extra, protocol)
        if tree_edit:
            tree_edit(tree)
        if archive_edit:
            payload = archive_edit(payload)
        commit = {'sha': SHA, 'commit': {'tree': {'sha': TREE}}}

        def environment(job):
            path = job / 'environment' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            path.parent.mkdir(parents=True)
            path.write_bytes(b'fake-python')
            return path

        first = {'commit': commit} if target.startswith('branch:') else [commit]
        with patch('bot_app.features.updates.engine.download', side_effect=[json.dumps(first).encode(), json.dumps(tree).encode(), payload]) as download, \
             patch.object(self.updater, 'prepare_environment', side_effect=environment) as install:
            self.download, self.install = download, install
            return self.updater.prepare(target)

    def assert_preserved(self):
        for name, data in LOCAL.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def test_branch_is_pinned_and_only_application_blobs_are_staged(self):
        ticket = self.prepare('branch:codex/haiyang-voice')
        urls = [call.args[0] for call in self.download.call_args_list]
        self.assertIn('/branches/codex%2Fhaiyang-voice', urls[0])
        self.assertEqual(urls[-1], f'https://codeload.github.com/{REPO}/zip/{SHA}')
        job = self.updater.job(ticket['id'])
        metadata = read_json(job / 'source/version.json')
        self.assertEqual(metadata['source']['commit'], SHA)
        self.assertIn(SHA[:12], ticket['label'])
        self.assertIn(MODULE, metadata['files'])
        for name in (*LOCAL, '.github/workflows/test.yml', 'assets/example.wav'):
            self.assertNotIn(name, metadata['files'])
            self.assertFalse((job / 'source' / name).exists())
        self.assert_preserved()

    def test_legacy_and_feature_layouts_are_supported(self):
        for required in (LEGACY_REQUIRED, REQUIRED):
            with self.subTest(required=required):
                self.assertIsNotNone(self.prepare(required=required))

    def test_missing_required_updater_is_rejected_before_dependencies(self):
        for required in (REQUIRED - {'bot_app/features/updates/engine.py'}, LEGACY_REQUIRED - {'bot_app/domain/updates.py'}):
            with self.subTest(required=required), self.assertRaises(UpdateError):
                self.prepare(required=required)
            self.install.assert_not_called()
        self.assert_preserved()

    def test_short_commit_resolves_to_full_immutable_commit(self):
        ticket = self.prepare(SHA[:8])
        self.assertIn('/commits?sha=' + SHA[:8] + '&per_page=1', self.download.call_args_list[0].args[0])
        plan = read_json(self.updater.job(ticket['id']) / 'plan.json')
        self.assertEqual(plan['source']['kind'], 'commit')
        self.assertEqual(plan['source']['commit'], SHA)

    def test_same_commit_does_not_redownload_or_prepare_environment(self):
        atomic_json(self.root / 'version.json', {'version': VERSION, 'source': {'commit': SHA, 'repository': REPO}})
        commit = {'sha': SHA, 'commit': {'tree': {'sha': TREE}}}
        with patch('bot_app.features.updates.engine.download', return_value=json.dumps({'commit': commit}).encode()) as download, \
             patch.object(self.updater, 'prepare_environment') as install:
            self.assertIsNone(self.updater.prepare('branch:main'))
        self.assertEqual(download.call_count, 1)
        install.assert_not_called()

    def test_bare_update_returns_source_build_to_same_version_stable_release(self):
        atomic_json(self.root / 'version.json', {'version': VERSION, 'source': {'commit': SHA, 'repository': REPO}})
        base = f'Discord-Music-Bot-{VERSION}-windows'
        payload = b'archive fixture verified before staging'
        checksum = (hashlib.sha256(payload).hexdigest() + f'  {base}.zip\n').encode()
        release = {'tag_name': VERSION, 'assets': [
            {'name': base + '.' + extension, 'state': 'uploaded',
             'browser_download_url': f'https://github.com/{REPO}/releases/download/{VERSION}/{base}.{extension}'}
            for extension in ('zip', 'sha256')]}
        with patch('bot_app.features.updates.engine.download', side_effect=[json.dumps(release).encode(), checksum, payload]), \
             patch.object(self.updater, '_stage', return_value={'version': VERSION}) as stage:
            self.assertEqual(self.updater.prepare(), {'version': VERSION})
        stage.assert_called_once_with(payload, tag=VERSION)

    def test_resolution_must_match_requested_short_sha(self):
        commit = {'sha': 'b' * 40, 'commit': {'tree': {'sha': TREE}}}
        with patch('bot_app.features.updates.engine.download', return_value=json.dumps([commit]).encode()) as download:
            with self.assertRaisesRegex(UpdateError, 'immutable'):
                self.updater.prepare(SHA[:8])
        self.assertEqual(download.call_count, 1)

    def test_incomplete_tree_and_symlinks_are_rejected(self):
        for edit in (lambda tree: tree.update(truncated=True),
                     lambda tree: next(row for row in tree['tree'] if row['path'] == 'main.py').update(mode='120000')):
            with self.subTest(edit=edit), self.assertRaises(UpdateError):
                self.prepare(tree_edit=edit)
            self.install.assert_not_called()
        self.assert_preserved()

    def test_archive_tampering_traversal_links_and_duplicate_paths_are_rejected(self):
        def changed(payload, name, symlink):
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(payload)) as original, zipfile.ZipFile(output, 'w') as edited:
                for info in original.infolist():
                    if info.filename != name:
                        edited.writestr(info, original.read(info))
                info = zipfile.ZipInfo(name)
                if symlink:
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                edited.writestr(info, b'tampered')
            return output.getvalue()

        for name, link in (('repository-root/main.py', False), ('repository-root/../escape.py', False),
                           ('repository-root/bot_app/link.py', True), ('repository-root/MAIN.PY', False)):
            with self.subTest(name=name), self.assertRaises(UpdateError):
                self.prepare(archive_edit=lambda data: changed(data, name, link))
            self.install.assert_not_called()
        self.assert_preserved()

    def test_unsupported_protocol_is_rejected_before_dependency_install(self):
        with self.assertRaises(UpdateError):
            self.prepare(protocol=2)
        self.install.assert_not_called()
        self.assert_preserved()

    def test_apply_and_rollback_preserve_all_local_state(self):
        ticket = self.prepare()
        self.updater.activate(ticket, 123)
        request = read_json(self.updater.pending)
        with patch('bot_app.features.updates.engine.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            self.updater.apply(request)
        self.assertEqual((self.root / MODULE).read_bytes(), b'# new music implementation\n')
        self.assertEqual(read_json(self.root / 'version.json')['source']['commit'], SHA)
        self.assertFalse((self.root / 'bot_app/obsolete.py').exists())
        self.assert_preserved()
        self.updater.rollback(request)
        self.assertEqual((self.root / 'main.py').read_bytes(), b'# original launcher')
        self.assertFalse(self.updater.active.exists())
        self.assert_preserved()

    def test_failed_check_restores_code_and_keeps_local_state(self):
        ticket = self.prepare()
        self.updater.activate(ticket, 123)
        with patch('bot_app.features.updates.engine.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(UpdateError):
                self.updater.apply(read_json(self.updater.pending))
        self.assertEqual((self.root / 'main.py').read_bytes(), b'# original launcher')
        self.assert_preserved()

    def test_ready_marker_checks_exact_source_commit(self):
        ticket = self.prepare()
        job = self.updater.job(ticket['id'])
        self.updater.mark_ready(ticket['id'])
        self.assertFalse((job / 'ready.json').exists())
        atomic_json(self.root / 'version.json', read_json(job / 'source/version.json'))
        self.updater.mark_ready(ticket['id'])
        self.assertTrue((job / 'ready.json').exists())


class SourceCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_command_passes_branch_and_commit_targets(self):
        from bot_app.host.discord.bot import create_bot
        bot = create_bot()
        self.addAsyncCleanup(bot.close)
        bot.updates.prepare = AsyncMock(return_value=None)
        ctx = SimpleNamespace(send=AsyncMock())
        with patch.dict(os.environ, {'BOT_SUPERVISED': '1'}):
            for target in ('branch:codex/haiyang-voice', SHA[:8]):
                await bot.get_command('update').callback(ctx, target)
                bot.updates.prepare.assert_awaited_with(target)
