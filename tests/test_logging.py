import asyncio
import logging
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot_app.application.diagnostics import log_scope, log_identity
from bot_app.application.music.service import PlaybackService
from bot_app.infrastructure.logging_setup import ScopedLogHandler, log_path, configure_asyncio_logging
from bot_app.infrastructure.persistence.logs import FileLogReader


class LoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.handler = ScopedLogHandler(self.root)
        self.addCleanup(self.handler.close)
        self.logger = logging.Logger('test.music', logging.DEBUG)
        self.logger.addHandler(self.handler)
        self.reader = FileLogReader(self.root / 'system.log')

    def test_scopes_are_isolated_and_restored(self):
        self.logger.info('system only')
        with log_scope(1, 11):
            self.logger.info('first server')
            with log_scope(2, 22):
                self.logger.info('second server')
            self.logger.info('first again')
        self.assertEqual(log_identity.get(), (None, None))
        self.assertEqual(len(self.reader.read(80)), 1)
        self.assertEqual(len(self.reader.read(80, 1)), 2)
        self.assertEqual(len(self.reader.read(80, 2)), 1)
        self.assertEqual(self.reader.read(80, 3), [])
        self.assertIn('channel=11', self.reader.read(80, 1)[0])

    def test_activity_is_clean_and_details_keep_traceback(self):
        with log_scope(1):
            self.logger.debug('extractor details')
            try:
                raise ValueError('Forbidden')
            except ValueError:
                self.logger.exception('\x1b[31mDownload failed\x1b[0m\n&#x20;retry')
        activity = self.reader.read(80, 1)
        self.assertEqual(len(activity), 1)
        self.assertIn('ValueError: Forbidden', activity[0])
        self.assertNotIn('\x1b', activity[0])
        self.assertNotIn('&#', activity[0])
        detail = '\n'.join(self.reader.read(80, 1, 'DEBUG', True))
        self.assertIn('Traceback', detail)
        self.assertIn('extractor details', detail)
        self.assertIn('guild=1 channel=None', detail)
        self.assertEqual(self.reader.read(80, 1, 'CRITICAL'), [])

    def test_repeated_activity_is_grouped_but_details_complete(self):
        with patch('bot_app.infrastructure.logging_setup.time.monotonic', side_effect=[0, 1, 2, 61]):
            for _ in range(4):
                self.logger.warning('retry failed')
        self.assertEqual(len(self.reader.read(80)), 2)
        self.assertIn('repeated 2 more times', self.reader.read(80)[-1])
        self.assertEqual(len(self.reader.read(80, details=True)), 4)

    def test_rotation_and_open_scope_limits(self):
        self.handler.max_bytes = 256
        self.handler.backups = 2
        self.handler.max_scopes = 2
        for guild in range(1, 5):
            with log_scope(guild):
                for index in range(15):
                    self.logger.info('%s %s', index, 'x' * 80)
        self.assertEqual(len(self.handler.routes), 2)
        for guild in range(1, 5):
            self.assertTrue(log_path(self.root, guild).is_file())
            self.assertLessEqual(len(list(log_path(self.root, guild).parent.iterdir())), 6)

    def test_path_traversal_is_rejected(self):
        for guild in ('../secret', '0', '-1', str(2**64), '１２'):
            with self.assertRaises(ValueError):
                self.reader.read(80, guild)


class WorkerContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_bot_restores_async_diagnostics_after_startup_failure(self):
        from bot_app.main import run_bot
        class OfflineBot:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            start = AsyncMock(side_effect=RuntimeError('startup transport failure'))
        loop = asyncio.get_running_loop()
        original = loop.get_exception_handler()
        previous = Mock()
        loop.set_exception_handler(previous)
        try:
            with patch.dict('os.environ', {'MANAGEMENT_API_ENABLED':'false'}), patch('bot_app.main.signal.signal'):
                with self.assertRaisesRegex(RuntimeError, 'startup transport failure'):
                    await run_bot(OfflineBot(), 'unused-offline-token')
            self.assertIs(loop.get_exception_handler(), previous)
        finally:
            loop.set_exception_handler(original)

    async def test_failed_task_is_reported_immediately_with_identity_and_traceback(self):
        service = PlaybackService.__new__(PlaybackService)
        service.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        service.background_tasks = set()
        async def fail():
            raise RuntimeError('checkpoint failure')
        with self.assertLogs('bot_app.application.music.sessions', level='ERROR') as captured:
            with log_scope(1, 11):
                task = service._spawn(fail(), name='music.checkpoints')
            with self.assertRaisesRegex(RuntimeError, 'checkpoint failure'):
                await task
        self.assertEqual(service.background_tasks, set())
        record = captured.records[0]
        self.assertEqual((record.guild_id, record.channel_id), (1, 11))
        self.assertIn('music.checkpoints', record.getMessage())
        self.assertIn('Traceback', captured.output[0])

    async def test_cancelled_owned_task_is_not_reported_as_a_bug(self):
        service = PlaybackService.__new__(PlaybackService)
        service.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        service.background_tasks = set()
        with self.assertNoLogs('bot_app.application.music.sessions', level='WARNING'):
            task = service._spawn(asyncio.Event().wait(), name='music.idle')
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(service.background_tasks, set())

    async def test_unowned_async_failure_has_task_name_and_original_traceback(self):
        loop = asyncio.get_running_loop()
        previous = configure_asyncio_logging(loop)
        try:
            try:
                raise RuntimeError('orphan failure')
            except RuntimeError as error:
                with self.assertLogs('bot.asyncio', level='ERROR') as captured:
                    loop.call_exception_handler({'message':'Task failed', 'exception':error,
                        'future':SimpleNamespace(get_name=lambda: 'unowned-task')})
            self.assertIn('unowned-task', captured.output[0])
            self.assertIn('Traceback', captured.output[0])
            self.assertIn('orphan failure', captured.output[0])
        finally:
            loop.set_exception_handler(previous)

    async def test_concurrent_media_workers_keep_their_server(self):
        service = PlaybackService.__new__(PlaybackService)
        service.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        with ThreadPoolExecutor(max_workers=2) as executor:
            service.executor = executor
            async def work(guild):
                with log_scope(guild, guild + 10):
                    await asyncio.sleep(0)
                    return await service.run_media(log_identity.get)
            self.assertEqual(await asyncio.gather(work(1), work(2)), [(1, 11), (2, 12)])
        self.assertEqual(log_identity.get(), (None, None))


class BootstrapLoggingTests(unittest.TestCase):
    def test_bot_construction_failure_is_logged_and_publishes_error_state(self):
        from bot_app import main as entry
        record = {'pid':123, 'created_at':1}
        with patch.object(entry, 'load_dotenv'), patch.object(entry, 'startup_problems', return_value=[]), \
             patch.object(entry, 'ProcessLock') as lock, \
             patch.object(entry, 'is_process_record_running', return_value=False), \
             patch.object(entry, 'read_process_record', side_effect=[None, record]), \
             patch.object(entry, 'write_process_record', return_value=record), \
             patch.object(entry, 'remove_process_record'), patch.object(entry, 'lower_windows_process_priority'), \
             patch.object(entry, 'write_status') as status, \
             patch.object(entry, 'create_bot', side_effect=RuntimeError('construction failed')), \
             patch('bot_app.infrastructure.logging_setup.configure_logging'), patch('logging.basicConfig'):
            with self.assertLogs('bot_app.main', level='INFO') as captured:
                self.assertEqual(entry.main([]), 1)
        self.assertTrue(any('Bot failed' in text and 'Traceback' in text for text in captured.output))
        self.assertTrue(any('Bot process starting' in text for text in captured.output))
        self.assertEqual(status.call_args.kwargs['state'], 'error')
        lock.return_value.release.assert_called_once()
