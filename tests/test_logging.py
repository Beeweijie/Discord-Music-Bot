import asyncio
import logging
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot_app.application.diagnostics import log_scope, log_identity
from bot_app.application.music.service import PlaybackService
from bot_app.infrastructure.logging_setup import ScopedLogHandler, log_path
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
