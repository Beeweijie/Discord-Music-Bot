import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import discord
from bot_app.application.music.models import Song, ChannelSession
from bot_app.application.music.recovery import RecoveryOperations
from bot_app.infrastructure.persistence.queues import QueueRepository
from bot_app.infrastructure.media.source import MediaSource
from bot_app.presentation.discord.panels import MusicPresenter, NowPlayingView
from bot_app.domain.music.errors import clean_media_error


class RetryTests(unittest.TestCase):
    def test_403_refreshes_extraction_and_bounds_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = MediaSource('ffmpeg', tmp)
            track = Song('https://www.youtube.com/watch?v=test', 'test', 1, 'user', True)
            track.cancel_event = Mock()
            track.cancel_event.is_set.return_value = False
            track.cancel_event.wait.return_value = False
            media._download_attempt = Mock(side_effect=[RuntimeError('HTTP Error 403'), Path(tmp)/'ok.mp3'])
            self.assertEqual(media._download_song(track), Path(tmp)/'ok.mp3')
            self.assertEqual([c.args[1] for c in media._download_attempt.call_args_list], [0, 1])
            media._download_attempt = Mock(side_effect=RuntimeError('\x1b[0;31mERROR:\x1b[0m HTTP Error 403 &#x20;'))
            with self.assertRaisesRegex(RuntimeError, 'skipping track') as error:
                media._download_song(track)
            self.assertNotIn('\x1b', str(error.exception))
            self.assertNotIn('&#', str(error.exception))
            self.assertEqual(media._download_attempt.call_count, 3)

    def test_non_403_and_cancellation_do_not_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = MediaSource('ffmpeg', tmp)
            track = Song('url', 'test', 1, 'user', True)
            media._download_attempt = Mock(side_effect=RuntimeError('private video'))
            with self.assertRaisesRegex(RuntimeError, 'private video'):
                media._download_song(track)
            self.assertEqual(media._download_attempt.call_count, 1)
            track.cancel_event.set()
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                media._download_song(track)
            self.assertEqual(media._download_attempt.call_count, 1)

    def test_failed_attempt_cleans_partial_files_and_reextracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = MediaSource('ffmpeg', tmp)
            track = Song('original', 'test', 1, 'user', True, webpage_url='fresh-page')
            def fail(url, download):
                (media.cache_dir/'fixed.part').write_bytes(b'partial')
                raise RuntimeError('403')
            with patch('bot_app.infrastructure.media.source.uuid.uuid4', return_value=SimpleNamespace(hex='fixed')), patch('bot_app.infrastructure.media.source.yt_dlp.YoutubeDL') as factory:
                downloader = factory.return_value.__enter__.return_value
                downloader.extract_info.side_effect = fail
                with self.assertRaises(RuntimeError):
                    media._download_attempt(track)
                downloader.extract_info.assert_called_once_with('fresh-page', download=True)
            self.assertFalse(list(media.cache_dir.iterdir()))
            self.assertFalse(media.download_prefixes())


class PersistenceTests(unittest.TestCase):
    def test_restart_restores_metadata_position_and_guild_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = RecoveryOperations()
            service.queue_repository = QueueRepository(tmp)
            service.closing = False
            service._session_status_key = lambda s: f'{s.guild_id}:{s.channel_id}'
            current = Song('https://www.youtube.com/watch?v=x', 'current', 3, 'user', True, duration=300)
            queued = Song('local', 'next', 4, 'other', False)
            session = ChannelSession(guild_id=1, channel_id=2, current_song=current, queue=[queued], position_seconds=90,
                                     volume=62, loop_mode='queue', panel_channel_id=6, panel_message_id=7)
            service._persist_session(session)
            service._persist_session(ChannelSession(guild_id=8, channel_id=9, queue=[queued]))
            service.sessions = {}
            service.restore_queues()
            restored = service.sessions['1:2']
            self.assertTrue(restored.restored)
            self.assertIsNone(restored.vc)
            self.assertEqual([s.title for s in restored.queue], ['current', 'next'])
            self.assertEqual(restored.queue[0].resume_at, 90)
            self.assertIsNone(restored.queue[0].local_path)
            self.assertEqual((restored.volume, restored.loop_mode, restored.panel_message_id), (62, 'queue', 7))
            self.assertEqual(len(service.sessions['8:9'].queue), 1)
            restored.stopped = True
            service._persist_session(restored)
            self.assertNotIn('1:2', service.queue_repository.load())
            self.assertIn('8:9', service.queue_repository.load())

    def test_corrupt_snapshot_does_not_prevent_other_guild_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = QueueRepository(tmp)
            repository.save('1:2', {'tracks': []})
            repository.path('1:3').write_text('{broken')
            with self.assertLogs('bot_app.infrastructure.persistence.queues', level='ERROR'):
                self.assertEqual(list(repository.load()), ['1:2'])
            self.assertTrue(repository.path('1:3').exists())


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.message = SimpleNamespace(id=12, edit=AsyncMock())
        self.channel = SimpleNamespace(id=10, get_partial_message=Mock(return_value=self.message), send=AsyncMock(return_value=self.message))
        self.presenter = MusicPresenter(SimpleNamespace(get_channel=lambda _: self.channel))
        self.session = ChannelSession(guild_id=1, channel_id=2, panel_channel_id=10, panel_message_id=12)
        self.service = SimpleNamespace(sessions={'1:2': self.session}, _session_status_key=lambda _: '1:2',
                                      _persist_session=Mock(), position=lambda _: 25)
        self.presenter.service = self.service

    async def test_transient_edit_keeps_message_id_and_retries_same_message(self):
        self.message.edit.side_effect = [discord.HTTPException(SimpleNamespace(status=503, reason='unavailable'), 'retry'), None]
        with self.assertLogs('bot_app.presentation.discord.panels', level='WARNING'):
            await self.presenter._refresh_now_playing_panel(self.session)
        self.assertEqual(self.session.panel_message_id, 12)
        await self.presenter._refresh_now_playing_panel(self.session)
        self.assertEqual(self.message.edit.await_count, 2)
        self.channel.send.assert_not_awaited()

    async def test_deleted_message_is_recreated(self):
        self.message.edit.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'missing')
        await self.presenter._refresh_now_playing_panel(self.session)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.session.panel_message_id, 12)

    async def test_song_switch_updates_cover_and_removes_old_animation(self):
        for title, cover in (("A", "https://i.ytimg.com/vi/a/hqdefault.jpg"), ("B", "https://i.ytimg.com/vi/b/hqdefault.jpg")):
            self.session.current_song = Song(title, title, 1, 'user', False, duration=120, thumbnail=cover)
            await self.presenter._refresh_now_playing_panel(self.session)
        kwargs = self.message.edit.call_args.kwargs
        self.assertIn('B', kwargs['embed'].description)
        self.assertEqual(kwargs['embed'].image.url, 'https://i.ytimg.com/vi/b/hqdefault.jpg')
        self.assertEqual(kwargs['attachments'], [])
        self.assertNotIn('播放进度', [field.name for field in kwargs['embed'].fields])
        self.assertIsNone(kwargs['view'].timeout)
        self.assertEqual(self.message.edit.await_count, 2)

    async def test_queued_edits_finish_with_latest_song(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_edit(**kwargs):
            entered.set()
            await release.wait()
        self.message.edit.side_effect = slow_edit
        self.session.current_song = Song('a', 'A', 1, 'user', False)
        first = asyncio.create_task(self.presenter._refresh_now_playing_panel(self.session))
        await entered.wait()
        self.session.current_song = Song('b', 'B', 1, 'user', False)
        second = asyncio.create_task(self.presenter._refresh_now_playing_panel(self.session))
        release.set()
        await asyncio.gather(first, second)
        self.assertIn('B', self.message.edit.call_args.kwargs['embed'].description)

    async def test_missing_cover_uses_youtube_thumbnail_or_no_image(self):
        song = Song('https://music.youtube.com/watch?v=abc123', 'test', 1, 'user', True)
        self.assertEqual(self.presenter._cover_url(song), 'https://i.ytimg.com/vi/abc123/hqdefault.jpg')
        song.input = 'local'
        self.assertIsNone(self.presenter._cover_url(song))
