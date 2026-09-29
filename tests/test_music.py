import asyncio
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot_app.application.music.models import ChannelSession, Song
from bot_app.application.music.service import PlaybackService
from bot_app.infrastructure.media.source import MediaSource
from bot_app.infrastructure.persistence.music_files import MusicFiles


def song(name="song", remote=False):
    return Song(name, name, 1, "member", remote)


class MusicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = asyncio.get_running_loop()
        self.bot = SimpleNamespace(loop=self.loop, user=SimpleNamespace(id=999), get_channel=lambda _: None)
        self.media = MediaSource("ffmpeg", Path(self.tmp.name))
        self.presenter = Mock()
        for name in ["_refresh_now_playing_panel", "_disable_now_playing_panel", "_send_or_update_now_playing_panel", "_safe_send"]:
            setattr(self.presenter, name, AsyncMock())
        self.runtime = Mock()
        self.runtime.drain_control_commands.return_value = []
        self.audio = Mock()
        self.service = PlaybackService(self.bot, self.media, self.presenter, self.audio,
                                       self.runtime, ThreadPoolExecutor(max_workers=2), MusicFiles(self.media.music_dir))
        self.addAsyncCleanup(self.service.close)
        self.vc = Mock()
        self.vc.is_connected.return_value = True
        self.vc.is_playing.return_value = True
        self.vc.is_paused.return_value = False
        self.vc.disconnect = AsyncMock()
        self.guild = SimpleNamespace(id=1, name="guild", voice_client=self.vc, me=None)
        self.channel = SimpleNamespace(id=2, name="voice", guild=self.guild, members=[])
        self.vc.guild = self.guild
        self.vc.channel = self.channel
        self.ctx = SimpleNamespace(author=SimpleNamespace(id=3, voice=SimpleNamespace(channel=self.channel)),
                                   guild=self.guild, channel=SimpleNamespace(id=4), send=AsyncMock())
        self.session = ChannelSession(vc=self.vc, guild_id=1, channel_id=2)
        self.service.sessions["1:2"] = self.session

    async def test_empty_play_uses_default_playlist_and_explicit_url_is_preserved(self):
        default = "https://music.youtube.com/playlist?list=PLH6zD0MCw2r4"
        explicit = "https://music.youtube.com/playlist?list=another"
        self.service._check_cooldown = AsyncMock(return_value=True)
        self.service._queue_collection_input = AsyncMock()
        for value in (None, "", "   ", explicit):
            with self.subTest(value=value):
                await self.service.dispatch("play", self.ctx, value)
                self.service._queue_collection_input.assert_awaited_with(
                    self.ctx, explicit if value == explicit else default, self.channel, self.session)

    async def test_seek_preserves_song_queue_and_ignores_old_callback(self):
        path = Path(self.tmp.name) / "audio.mp3"
        path.write_bytes(b"audio")
        current = song("current", remote=True)
        current.local_path = path
        current.duration = 180
        self.session.current_song = current
        queued = song("next")
        self.session.queue.append(queued)
        self.service.play_next = AsyncMock()
        for paused in (False, True):
            self.vc.is_paused.return_value = paused
            old_id = self.session.playback_id
            await self.service.execute_control({"action": "seek", "session_id": "1:2", "position": "1:30"})
            self.audio.create_source.assert_called_with(path, 40, position=90)
            await self.service._after_playback("1:2", self.session, current, self.session.generation, None, old_id)
            self.assertIs(self.session.current_song, current)
            self.assertEqual(self.session.queue, [queued])
            self.assertEqual(current.local_path, path)
            self.service.play_next.assert_not_awaited()
        self.vc.pause.assert_called_once()
        await self.service.dispatch("seek", self.ctx, "0")
        self.audio.create_source.assert_called_with(path, 40, position=0)
        self.ctx.send.assert_awaited_with("Seeked to 0:00")

    async def test_seek_rejects_invalid_time_and_missing_playback(self):
        from bot_app.domain.control import validate_control
        for position in ("-1", "1:60", "abc", True, None, "90000"):
            with self.subTest(position=position), self.assertRaises(ValueError):
                validate_control({"action": "seek", "session_id": "1:2", "position": position})
        with self.assertRaises(ValueError):
            await self.service.execute_control({"action": "seek", "session_id": "1:2", "position": 90})
        self.session.current_song = song()
        self.session.current_song.duration = 60
        with self.assertRaises(ValueError):
            await self.service.execute_control({"action": "seek", "session_id": "1:2", "position": 90})
        self.vc.stop.assert_not_called()

    async def test_guild_specific_playlist_and_volume(self):
        from bot_app.infrastructure.persistence.settings import JsonSettingsRepository
        repo = JsonSettingsRepository(Path(self.tmp.name) / "config")
        self.service.settings_repository = repo
        settings = repo.read("music", 1)
        custom = "https://music.youtube.com/playlist?list=custom"
        repo.write("music", dict(settings, default_playlist_url=custom, default_volume=17), 1)
        repo.write("music", dict(settings, default_volume=63), 2)
        self.service._check_cooldown = AsyncMock(return_value=True)
        self.service._queue_collection_input = AsyncMock()
        await self.service.dispatch("play", self.ctx, None)
        self.service._queue_collection_input.assert_awaited_with(self.ctx, custom, self.channel, self.session)
        for guild_id, volume in ((1, 17), (2, 63)):
            channel = SimpleNamespace(id=99, guild=SimpleNamespace(id=guild_id))
            self.assertEqual(self.service._get_session(channel).volume, volume)
        self.assertNotEqual(self.service.settings_for(2)["default_playlist_url"], custom)

    async def test_failed_download_skips_to_next_without_stale_preparing_song(self):
        first, second = song("bad", True), song("good", True)
        path = Path(self.tmp.name) / "good.mp3"
        path.write_bytes(b"audio")
        self.session.queue = [first, second]
        self.vc.is_playing.return_value = False
        self.service._download_song_limited = AsyncMock(side_effect=[RuntimeError("HTTP 403"), path])
        self.service._get_text_channel = Mock(return_value=SimpleNamespace(send=AsyncMock()))
        self.service._start_predownload_task = Mock()
        await self.service.play_next("1:2")
        self.assertIs(self.session.current_song, second)
        self.assertIsNone(self.session.preparing_song)
        self.assertEqual(self.session.queue, [])
        self.vc.play.assert_called_once()

    async def test_empty_play_resumes_restored_queue_without_adding_default_playlist(self):
        self.session.restored = True
        self.session.queue = [song("saved")]
        self.service._continue_saved = AsyncMock()
        self.service._queue_collection_input = AsyncMock()
        await self.service.dispatch("play", self.ctx, None)
        self.service._continue_saved.assert_awaited_once_with(self.ctx, self.session)
        self.service._queue_collection_input.assert_not_awaited()

    async def test_desktop_skip_and_discord_skip_use_playback(self):
        await self.service.execute_control({"action": "skip", "session_id": "1:2"})
        self.vc.stop.assert_called_once()
        self.vc.stop.reset_mock()
        await self.service.dispatch("skip", self.ctx)
        self.vc.stop.assert_called_once()

    async def test_volume_loop_pause_and_resume_controls(self):
        await self.service.execute_control({"action":"volume", "session_id":"1:2", "volume":65})
        self.audio.set_volume.assert_called_with(self.vc, 65)
        await self.service.execute_control({"action":"loop", "session_id":"1:2", "mode":"queue"})
        self.assertEqual(self.session.loop_mode, "queue")
        await self.service.execute_control({"action":"pause", "session_id":"1:2"})
        self.vc.pause.assert_called_once()
        self.vc.is_paused.return_value = True
        await self.service.execute_control({"action":"resume", "session_id":"1:2"})
        self.vc.resume.assert_called_once()

    async def test_clear_preserves_current_and_cancels_queued_downloads(self):
        current, queued = song("current"), song("queued", True)
        self.session.current_song = current
        self.session.queue = [queued]
        await self.service.execute_control({"action":"clear", "session_id":"1:2"})
        self.assertIs(self.session.current_song, current)
        self.assertEqual(self.session.queue, [])
        self.assertTrue(queued.cancel_event.is_set())

    async def test_stop_invalidates_old_playback_callback(self):
        current = song()
        self.session.current_song = current
        generation = self.session.generation
        await self.service.execute_control({"action":"stop", "session_id":"1:2"})
        self.assertTrue(self.session.stopped)
        self.assertNotIn("1:2", self.service.sessions)
        self.service.play_next = AsyncMock()
        await self.service._after_playback("1:2", self.session, current, generation, None)
        self.service.play_next.assert_not_awaited()

    async def test_control_lock_serializes_desktop_and_discord_mutations(self):
        await self.session.control_lock.acquire()
        desktop = asyncio.create_task(self.service.execute_control({"action":"volume", "session_id":"1:2", "volume":20}))
        discord = asyncio.create_task(self.service.dispatch("volume", self.ctx, 30))
        await asyncio.sleep(0)
        self.assertFalse(desktop.done())
        self.assertFalse(discord.done())
        self.session.control_lock.release()
        await asyncio.gather(desktop, discord)
        self.assertEqual(self.session.volume, 30)

    async def test_download_waiter_cancellation_does_not_duplicate_worker(self):
        started, release = threading.Event(), threading.Event()
        path = Path(self.tmp.name) / "result.mp3"
        path.write_bytes(b"audio")
        worker_threads = []
        def download(track):
            worker_threads.append(threading.get_ident())
            started.set()
            release.wait(3)
            return path
        self.media._download_song = Mock(side_effect=download)
        track = song("remote", True)
        first = asyncio.create_task(self.service._download_song_limited(track))
        try:
            while not started.is_set():
                await asyncio.sleep(0.01)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(track.download_task.cancelled())
            release.set()
            self.assertEqual(await self.service._download_song_limited(track), path)
            self.media._download_song.assert_called_once()
            self.assertNotEqual(worker_threads[0], threading.get_ident())
            self.assertEqual(track.local_path, path)
        finally:
            release.set()

    async def test_late_metadata_after_stop_does_not_repopulate_session(self):
        entered, release = threading.Event(), threading.Event()
        def lookup(*args):
            entered.set()
            release.wait(3)
            return song("late")
        self.media._search_youtube_song = lookup
        task = asyncio.create_task(self.service.dispatch("play", self.ctx, "query"))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            await self.service.execute_control({"action":"stop", "session_id":"1:2"})
            release.set()
            await task
            self.assertEqual(self.session.queue, [])
            self.assertNotIn("1:2", self.service.sessions)
        finally:
            release.set()

    async def test_queue_move_remove_and_member_channel_check(self):
        a, b, c = song("a"), song("b"), song("c")
        self.session.queue = [a,b,c]
        await self.service.dispatch("move", self.ctx, 3, 1)
        self.assertEqual(self.session.queue, [c,a,b])
        await self.service.dispatch("remove", self.ctx, 2)
        self.assertEqual(self.session.queue, [c,b])
        self.ctx.author.voice = None
        await self.service.dispatch("skip", self.ctx)
        self.vc.stop.assert_not_called()


    async def test_discord_volume_applies_to_live_audio(self):
        await self.service.dispatch("volume", self.ctx, 23)
        self.audio.set_volume.assert_called_once_with(self.vc, 23)
        self.assertEqual(self.session.volume, 23)
        await self.service.dispatch("volume", self.ctx, 101)
        self.assertEqual(self.session.volume, 23)

    async def test_skip_does_not_repeat_when_looping(self):
        self.session.current_song = song("current")
        self.session.loop_mode = "one"
        current = self.session.current_song
        await self.service.dispatch("skip", self.ctx)
        self.assertTrue(current.suppress_repeat)
        self.service.play_next = AsyncMock()
        await self.service._after_playback("1:2", self.session, current, 0, None)
        self.assertEqual(self.session.queue, [])
        self.assertEqual(self.session.history[0]["title"], "current")

    async def test_queue_edits_reject_stale_web_positions(self):
        self.session.queue = [song("one"), song("two"), song("three")]
        self.service._start_predownload_task = Mock()
        version = self.service._queue_version(self.session)
        await self.service.execute_control({"action":"nextup", "session_id":"1:2", "index":3, "queue_version":version})
        self.assertEqual([s.title for s in self.session.queue], ["three", "one", "two"])
        with self.assertRaisesRegex(ValueError, "queue changed"):
            await self.service.execute_control({"action":"remove", "session_id":"1:2", "index":1, "queue_version":version})
        self.assertEqual(len(self.session.queue), 3)

    async def test_fair_queue_dedupe_and_surprise_never_interrupt_current(self):
        current = song("playing")
        self.session.current_song = current
        self.session.queue = [song("a"), song("b"), song("c"), song("a")]
        self.session.queue[2].requester_id = 2
        self.service._start_predownload_task = Mock()
        await self.service.dispatch("fair", self.ctx)
        self.assertEqual([s.title for s in self.session.queue], ["a", "c", "b", "a"])
        removed = self.session.queue[-1]
        await self.service.dispatch("dedupe", self.ctx)
        self.assertTrue(removed.cancel_event.is_set())
        self.assertEqual(len(self.session.queue), 3)
        await self.service.dispatch("surprise", self.ctx)
        self.assertEqual(sorted(s.title for s in self.session.queue), ["a", "b", "c"])
        self.assertIs(self.session.current_song, current)
        self.vc.stop.assert_not_called()

    async def test_libraries_survive_restart_and_are_private_to_guild_user(self):
        from bot_app.infrastructure.persistence.library import LibraryRepository
        root = Path(self.tmp.name) / "library"
        self.service.library_repository = LibraryRepository(root)
        self.session.current_song = song("current")
        self.session.queue = [song("next")]
        await self.service.dispatch("favorite", self.ctx)
        await self.service.dispatch("favorite", self.ctx)
        await self.service.dispatch("playlist_save", self.ctx, "My playlist")
        fresh = LibraryRepository(root)
        data = fresh.read(1, 3)
        self.assertEqual(len(data["favorites"]), 1)
        self.assertEqual([s["title"] for s in data["playlists"]["My playlist"]], ["current", "next"])
        self.assertEqual(fresh.read(2, 3)["favorites"], [])
        self.assertEqual(fresh.read(1, 4)["favorites"], [])
        await self.service.dispatch("playlist_delete", self.ctx, "My playlist")
        self.assertEqual(fresh.read(1, 3)["playlists"], {})
        self.assertEqual(len(self.session.queue), 1)
        self.vc.stop.assert_not_called()

    async def test_loading_library_respects_queue_limit_and_requester(self):
        from bot_app.infrastructure.persistence.library import LibraryRepository
        from bot_app.domain.music.library import track_data
        self.service.library_repository = LibraryRepository(Path(self.tmp.name)/"library")
        self.service.library_repository.save(1, 3, {"favorites":[track_data(song("a")), track_data(song("b"))], "playlists":{}})
        self.service.settings["max_queue_size"] = 2
        self.session.queue = [song("existing")]
        self.service._ensure_connected = AsyncMock(return_value=self.vc)
        self.service._start_predownload_task = Mock()
        await self.service.dispatch("playlist_load", self.ctx, "favorites")
        self.assertEqual([s.title for s in self.session.queue], ["existing", "a"])
        self.assertEqual(self.session.queue[-1].requester_id, 3)

    async def test_sleep_timer_pauses_and_keeps_queue(self):
        self.session.current_song = song("current")
        self.session.queue = [song("next")]
        with patch("bot_app.application.music.extras.time.time", return_value=100):
            await self.service.dispatch("sleep", self.ctx, 1)
        with patch("bot_app.application.music.extras.time.time", return_value=159):
            await self.service._check_sleep(self.session)
        self.vc.pause.assert_not_called()
        with patch("bot_app.application.music.extras.time.time", return_value=160):
            await self.service._check_sleep(self.session)
        self.vc.pause.assert_called_once()
        self.assertTrue(self.session.sleeping)
        self.assertIsNone(self.session.sleep_until)
        self.assertEqual(len(self.session.queue), 1)
        self.vc.is_playing.return_value = False
        await self.service.play_next("1:2")
        self.assertEqual(len(self.session.queue), 1)
        self.service._resume_session(self.session)
        self.assertFalse(self.session.sleeping)

    async def test_paused_disconnect_after_ten_minutes_preserves_position(self):
        from bot_app.infrastructure.persistence.queues import QueueRepository
        self.service.queue_repository = QueueRepository(Path(self.tmp.name)/"queues")
        self.session.current_song = song("current")
        self.session.position_seconds = 73
        self.session.queue = [song("next")]
        self.session.paused_since = 100
        self.vc.is_playing.return_value = False
        self.vc.is_paused.return_value = True
        with patch("bot_app.application.music.sessions.asyncio.sleep", new_callable=AsyncMock), patch("bot_app.application.music.sessions.time.monotonic", return_value=700):
            await self.service._idle_watch_loop("1:2")
        parked = self.service.sessions["1:2"]
        self.assertIsNot(parked, self.session)
        self.assertTrue(parked.restored)
        self.assertIsNone(parked.vc)
        self.assertEqual([s.title for s in parked.queue], ["current", "next"])
        self.assertEqual(parked.queue[0].resume_at, 73)
        self.vc.disconnect.assert_awaited_once()
        self.assertEqual(self.service.queue_repository.load()["1:2"]["tracks"][0]["resume_at"], 73)
        self.service._persist_session(self.session)
        self.assertEqual(self.service.queue_repository.load()["1:2"]["tracks"][0]["resume_at"], 73)

    async def test_pause_clock_resets_on_resume(self):
        with patch("bot_app.application.music.recovery.time.monotonic", return_value=10):
            self.service._pause_session(self.session)
        self.assertEqual(self.session.paused_since, 10)
        self.service._resume_session(self.session)
        self.assertIsNone(self.session.paused_since)
        with patch("bot_app.application.music.recovery.time.monotonic", return_value=20):
            self.service._pause_session(self.session)
        self.assertEqual(self.session.paused_since, 20)

    async def test_pause_does_not_disconnect_before_ten_minutes(self):
        self.session.paused_since = 100
        self.session.current_song = song()
        self.vc.is_paused.return_value = True
        self.channel.members = [SimpleNamespace(bot=False)]
        with patch("bot_app.application.music.sessions.asyncio.sleep", new_callable=AsyncMock, side_effect=[None, asyncio.CancelledError()]), patch("bot_app.application.music.sessions.time.monotonic", return_value=699):
            with self.assertRaises(asyncio.CancelledError):
                await self.service._idle_watch_loop("1:2")
        self.vc.disconnect.assert_not_awaited()
        self.assertIs(self.service.sessions["1:2"], self.session)

    async def test_play_cannot_reuse_session_while_disconnect_is_pending(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def disconnect(**kwargs):
            started.set()
            await finish.wait()
        self.vc.disconnect = disconnect
        self.session.queue = [song("saved")]
        task = asyncio.create_task(self.service._dispose_session("1:2", preserve=True))
        await started.wait()
        try:
            with self.assertRaisesRegex(ValueError, "Disconnecting"):
                self.service._get_session(self.channel)
        finally:
            finish.set()
            await task
        self.assertTrue(self.service.sessions["1:2"].restored)
        self.assertFalse(self.service.sessions["1:2"].retiring)

    async def test_relative_seek_uses_current_position(self):
        self.session.current_song = song("current")
        self.session.position_seconds = 45
        self.service._seek_session = AsyncMock()
        await self.service.dispatch("seek", self.ctx, "+30")
        self.service._seek_session.assert_awaited_with("1:2", self.session, 75)
        await self.service.dispatch("seek", self.ctx, "-90")
        self.service._seek_session.assert_awaited_with("1:2", self.session, 0)

    async def test_web_resume_of_saved_queue_connects_only_when_someone_is_present(self):
        self.session.vc = None
        self.session.restored = True
        self.session.queue = [song("saved")]
        self.bot.get_channel = lambda _: self.channel
        with self.assertRaisesRegex(ValueError, "Join the original"):
            await self.service.execute_control({"action":"resume", "session_id":"1:2"})
        self.channel.members = [SimpleNamespace(bot=False)]
        self.service.play_next = AsyncMock()
        await self.service.execute_control({"action":"resume", "session_id":"1:2"})
        await asyncio.sleep(0)
        self.assertIs(self.session.vc, self.vc)
        self.assertFalse(self.session.restored)
        self.service.play_next.assert_awaited_once_with("1:2")


class MediaTests(unittest.TestCase):
    def test_local_file_scope_and_url_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test.mp3").write_bytes(b"audio")
            media = MediaSource("ffmpeg", root)
            self.assertEqual(media._resolve_local_music("test"), root / "test.mp3")
            for value in ["../test", "C:/test", "sub/test"]:
                self.assertIsNone(media._resolve_local_music(value))
            self.assertEqual(media._local_music_choices("test")[0].value, "test")

    def test_download_worker_returns_path_without_mutating_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = MediaSource("ffmpeg", Path(tmp))
            track = song("https://example.com/audio", True)
            result = Path(tmp) / "cache/result.mp3"
            result.write_bytes(b"audio")
            media._locate_downloaded_file = Mock(return_value=result)
            with patch("bot_app.infrastructure.media.source.yt_dlp.YoutubeDL") as ydl:
                ydl.return_value.__enter__.return_value.extract_info.return_value = {}
                self.assertEqual(media._download_song(track), result)
            self.assertIsNone(track.local_path)
            self.assertFalse(track.downloaded)
            self.assertEqual(media.download_prefixes(), set())
