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
        self.service.sessions["1"] = self.session

    async def test_failed_checkpoint_does_not_stop_other_guilds_or_next_interval(self):
        from bot_app.application.diagnostics import log_identity
        self.service.checkpoint_task.cancel()
        await asyncio.gather(self.service.checkpoint_task, return_exceptions=True)
        other = ChannelSession(guild_id=8, channel_id=9)
        self.service.sessions['8'] = other
        self.service._persist_session = Mock()
        visited = []
        async def check(session):
            visited.append(log_identity.get())
            if len(visited) == 1:
                raise RuntimeError('timer check failed')
        self.service._check_sleep = check
        with patch('bot_app.application.music.recovery.asyncio.sleep',
                   new_callable=AsyncMock, side_effect=[None, None, asyncio.CancelledError()]):
            with self.assertLogs('bot_app.application.music.recovery', level='ERROR') as captured:
                with self.assertRaises(asyncio.CancelledError):
                    await self.service._checkpoint_loop()
        self.assertEqual(visited, [(1, 2), (8, 9), (1, 2), (8, 9)])
        self.assertIn('retry next interval', captured.output[0])
        self.assertIn('Traceback', captured.output[0])
        self.assertEqual(self.service._persist_session.call_count, 3)
        self.assertEqual(log_identity.get(), (None, None))

    async def test_voice_connection_failure_has_traceback_in_server_log(self):
        self.session.vc = None
        self.guild.voice_client = None
        self.channel.connect = AsyncMock(side_effect=RuntimeError('handshake failed'))
        with self.assertLogs('bot_app.application.music.playback', level='WARNING') as captured:
            self.assertIsNone(await self.service._ensure_connected(self.ctx, self.channel, self.session))
        self.assertIn('Voice connection failed', captured.output[0])
        self.assertIn('Traceback', captured.output[0])
        self.assertEqual((captured.records[0].guild_id, captured.records[0].channel_id), (1, 2))
        self.ctx.send.assert_awaited_once()

    async def test_failed_search_is_logged_as_well_as_replied_to_user(self):
        self.service.run_media = AsyncMock(side_effect=RuntimeError('search backend failed'))
        with self.assertLogs('bot_app.application.music.commands', level='WARNING') as captured:
            await self.service.dispatch('play', self.ctx, 'search query')
        self.assertIn('Track search failed', captured.output[0])
        self.assertIn('Traceback', captured.output[0])
        self.assertEqual((captured.records[0].guild_id, captured.records[0].channel_id), (1, 2))
        self.ctx.send.assert_awaited_with('❌ Search failed:search backend failed')

    async def test_empty_play_uses_default_playlist_and_explicit_url_is_preserved(self):
        default = "https://music.youtube.com/playlist?list=PLH6zD0MCw2r4"
        explicit = "https://music.youtube.com/playlist?list=another"
        self.service._check_cooldown = AsyncMock(return_value=True)
        self.service._queue_collection_input = AsyncMock()
        for value in (None, "", "   ", explicit):
            with self.subTest(value=value):
                await self.service.dispatch("play", self.ctx, value)
                self.service._queue_collection_input.assert_awaited_with(
                    self.ctx, explicit if value == explicit else default, self.channel, self.session, front=False)

    async def test_playtop_inserts_first_preserves_playback_and_saves_order(self):
        current, first, second, priority = [song(name) for name in ("current", "first", "second", "priority")]
        self.service.run_media = AsyncMock(return_value=priority)
        self.service._start_predownload_task = Mock()
        self.service.play_next = AsyncMock()
        self.service.queue_repository = Mock()
        for paused in (False, True):
            with self.subTest(paused=paused):
                self.service.command_cooldowns.clear()
                self.session.current_song = current
                self.session.queue = [first, second]
                self.session.position_seconds = 42
                self.vc.is_playing.return_value = not paused
                self.vc.is_paused.return_value = paused
                await self.service.dispatch("playtop", self.ctx, "priority")
                self.assertEqual(self.session.queue, [priority, first, second])
                self.assertIs(self.session.current_song, current)
                self.assertEqual(self.session.position_seconds, 42)
                self.vc.stop.assert_not_called()
                self.vc.resume.assert_not_called()
                self.vc.play.assert_not_called()
                self.service.play_next.assert_not_awaited()
                self.presenter._refresh_now_playing_panel.assert_awaited_with(self.session)
                self.service._start_predownload_task.assert_called_with("1")
                saved = self.service.queue_repository.save.call_args.args[1]
                self.assertEqual([track["title"] for track in saved["tracks"]],
                                 ["current", "priority", "first", "second"])
                self.ctx.send.assert_any_await("✅ Added to the front of the queue: **priority** (position 1)")

    async def test_playtop_resolves_search_url_and_local_input_like_play(self):
        (self.media.music_dir / "local.mp3").write_bytes(b"audio")
        priority = song("priority")
        self.service.run_media = AsyncMock(return_value=priority)
        self.service._start_predownload_task = Mock()
        for query, resolver in (("a song name", self.service._search_youtube_song),
                                ("https://www.youtube.com/watch?v=abcdefghijk", self.service._get_song_for_input),
                                ("local.mp3", self.service._get_song_for_input)):
            with self.subTest(query=query):
                self.service.command_cooldowns.clear()
                self.session.queue = [song("waiting")]
                await self.service.dispatch("playtop", self.ctx, query)
                self.service.run_media.assert_awaited_with(resolver, query, self.ctx.author)
                self.assertIs(self.session.queue[0], priority)

    async def test_playtop_starts_new_track_when_idle(self):
        (self.media.music_dir / "priority.mp3").write_bytes(b"audio")
        priority, waiting = song("priority"), song("waiting")
        self.service.run_media = AsyncMock(return_value=priority)
        self.service._start_predownload_task = Mock()
        self.vc.is_playing.return_value = False
        for queue in ([], [waiting]):
            with self.subTest(queued=bool(queue)):
                self.service.command_cooldowns.clear()
                self.session.current_song = None
                self.session.queue = list(queue)
                self.vc.play.reset_mock()
                await self.service.dispatch("playtop", self.ctx, "priority.mp3")
                self.assertIs(self.session.current_song, priority)
                self.assertEqual(self.session.queue, queue)
                self.vc.play.assert_called_once()
                self.vc.stop.assert_not_called()

    async def test_playtop_collection_preserves_order_and_applies_limits(self):
        current, waiting, first, second, third = [song(name) for name in ("current", "waiting", "first", "second", "third")]
        self.session.current_song = current
        self.service.run_media = AsyncMock(return_value=[current, waiting, first, first, second, third])
        self.service._start_predownload_task = Mock()
        for url in ("https://www.youtube.com/playlist?list=example", "https://www.bilibili.com/video/BV1example"):
            for playlist_limit, queue_limit in ((2, 10), (10, 3)):
                with self.subTest(url=url, playlist_limit=playlist_limit, queue_limit=queue_limit):
                    self.service.command_cooldowns.clear()
                    self.session.queue = [waiting]
                    self.service.settings.update(max_playlist_songs=playlist_limit, max_queue_size=queue_limit)
                    await self.service.dispatch("playtop", self.ctx, url)
                    self.assertEqual(self.session.queue, [first, second, waiting])
                    self.assertIs(self.session.current_song, current)
                    self.vc.stop.assert_not_called()
                    self.assertIn("2** tracks to the front of the queue", self.ctx.send.await_args.args[0])

    async def test_playtop_single_track_collection_reports_first_position(self):
        priority, waiting = song("priority"), song("waiting")
        self.session.queue = [waiting]
        self.service.run_media = AsyncMock(return_value=[priority])
        self.service._start_predownload_task = Mock()
        await self.service.dispatch("playtop", self.ctx, "https://www.bilibili.com/video/BV1example")
        self.assertEqual(self.session.queue, [priority, waiting])
        self.ctx.send.assert_any_await("✅ Added to the front of the queue: **priority** (position 1)")

    async def test_playtop_rejects_full_queue_before_and_after_lookup(self):
        waiting = song("waiting")
        self.service.settings["max_queue_size"] = 1
        self.session.queue = [waiting]
        self.service.run_media = AsyncMock(return_value=song("priority"))
        await self.service.dispatch("playtop", self.ctx, "priority")
        self.service.run_media.assert_not_awaited()
        self.assertEqual(self.session.queue, [waiting])
        self.service.command_cooldowns.clear()
        self.session.queue = []
        async def fill_queue(*args):
            self.session.queue.append(waiting)
            return song("priority")
        self.service.run_media.side_effect = fill_queue
        await self.service.dispatch("playtop", self.ctx, "priority")
        self.assertEqual(self.session.queue, [waiting])
        self.ctx.send.assert_awaited_with("Queue full (limit: 1 tracks).")

    async def test_playtop_requires_input_and_voice_channel(self):
        self.service.run_media = AsyncMock()
        self.service._continue_saved = AsyncMock()
        self.session.restored = True
        self.session.queue = [song("saved")]
        await self.service.dispatch("playtop", self.ctx, "   ")
        self.ctx.send.assert_awaited_with("Provide a song name or URL to add to the front of the queue.")
        self.ctx.author.voice = None
        await self.service.dispatch("playtop", self.ctx, "priority")
        self.ctx.send.assert_awaited_with("Join a voice channel first.")
        self.service.run_media.assert_not_awaited()
        self.service._continue_saved.assert_not_awaited()
        self.assertEqual([track.title for track in self.session.queue], ["saved"])

    async def test_playtop_shares_play_cooldown_and_play_still_appends(self):
        waiting, added = song("waiting"), song("added")
        self.service.run_media = AsyncMock(return_value=added)
        self.service._start_predownload_task = Mock()
        with patch("bot_app.application.music.sessions.time.monotonic", return_value=100):
            for first_action, blocked_action in (("play", "playtop"), ("playtop", "play")):
                with self.subTest(first_action=first_action):
                    self.service.command_cooldowns.clear()
                    self.service.run_media.reset_mock()
                    self.session.queue = [waiting]
                    await self.service.dispatch(first_action, self.ctx, "added")
                    await self.service.dispatch(blocked_action, self.ctx, "blocked")
                    self.service.run_media.assert_awaited_once()
                    expected = [waiting, added] if first_action == "play" else [added, waiting]
                    self.assertEqual(self.session.queue, expected)

    async def test_stop_during_playtop_lookup_stays_responsive_and_prevents_late_enqueue(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def lookup(*args):
            entered.set()
            await release.wait()
            return song("late")
        self.service.run_media = AsyncMock(side_effect=lookup)
        task = asyncio.create_task(self.service.dispatch("playtop", self.ctx, "priority"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(self.service.execute_control({"action": "stop", "session_id": "1"}), 1)
        finally:
            release.set()
            await task
        self.assertEqual(self.session.queue, [])
        self.assertNotIn("1", self.service.sessions)

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
            await self.service.execute_control({"action": "seek", "session_id": "1", "position": "1:30"})
            self.audio.create_source.assert_called_with(path, 40, position=90)
            await self.service._after_playback("1", self.session, current, self.session.generation, None, old_id)
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
                validate_control({"action": "seek", "session_id": "1", "position": position})
        with self.assertRaises(ValueError):
            await self.service.execute_control({"action": "seek", "session_id": "1", "position": 90})
        self.session.current_song = song()
        self.session.current_song.duration = 60
        with self.assertRaises(ValueError):
            await self.service.execute_control({"action": "seek", "session_id": "1", "position": 90})
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
        self.service._queue_collection_input.assert_awaited_with(self.ctx, custom, self.channel, self.session, front=False)
        # Changing defaults does not replace the existing server player's volume.
        for guild_id, volume in ((1, 40), (2, 63)):
            channel = SimpleNamespace(id=99, guild=SimpleNamespace(id=guild_id, voice_client=None))
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
        await self.service.play_next("1")
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
        await self.service.execute_control({"action": "skip", "session_id": "1"})
        self.vc.stop.assert_called_once()
        self.vc.stop.reset_mock()
        await self.service.dispatch("skip", self.ctx)
        self.vc.stop.assert_called_once()

    async def test_volume_loop_pause_and_resume_controls(self):
        await self.service.execute_control({"action":"volume", "session_id":"1", "volume":65})
        self.audio.set_volume.assert_called_with(self.vc, 65)
        await self.service.execute_control({"action":"loop", "session_id":"1", "mode":"queue"})
        self.assertEqual(self.session.loop_mode, "queue")
        await self.service.execute_control({"action":"pause", "session_id":"1"})
        self.vc.pause.assert_called_once()
        self.vc.is_paused.return_value = True
        await self.service.execute_control({"action":"resume", "session_id":"1"})
        self.vc.resume.assert_called_once()

    async def test_clear_preserves_current_and_cancels_queued_downloads(self):
        current, queued = song("current"), song("queued", True)
        self.session.current_song = current
        self.session.queue = [queued]
        await self.service.execute_control({"action":"clear", "session_id":"1"})
        self.assertIs(self.session.current_song, current)
        self.assertEqual(self.session.queue, [])
        self.assertTrue(queued.cancel_event.is_set())

    async def test_stop_invalidates_old_playback_callback(self):
        current = song()
        self.session.current_song = current
        generation = self.session.generation
        await self.service.execute_control({"action":"stop", "session_id":"1"})
        self.assertTrue(self.session.stopped)
        self.assertNotIn("1", self.service.sessions)
        self.service.play_next = AsyncMock()
        await self.service._after_playback("1", self.session, current, generation, None)
        self.service.play_next.assert_not_awaited()

    async def test_control_lock_serializes_desktop_and_discord_mutations(self):
        await self.session.control_lock.acquire()
        desktop = asyncio.create_task(self.service.execute_control({"action":"volume", "session_id":"1", "volume":20}))
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
            await self.service.execute_control({"action":"stop", "session_id":"1"})
            release.set()
            await task
            self.assertEqual(self.session.queue, [])
            self.assertNotIn("1", self.service.sessions)
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
        await self.service._after_playback("1", self.session, current, 0, None)
        self.assertEqual(self.session.queue, [])
        self.assertEqual(self.session.history[0]["title"], "current")

    async def test_queue_edits_reject_stale_web_positions(self):
        self.session.queue = [song("one"), song("two"), song("three")]
        self.service._start_predownload_task = Mock()
        version = self.service._queue_version(self.session)
        await self.service.execute_control({"action":"nextup", "session_id":"1", "index":3, "queue_version":version})
        self.assertEqual([s.title for s in self.session.queue], ["three", "one", "two"])
        with self.assertRaisesRegex(ValueError, "queue changed"):
            await self.service.execute_control({"action":"remove", "session_id":"1", "index":1, "queue_version":version})
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
        await self.service.play_next("1")
        self.assertEqual(len(self.session.queue), 1)
        self.service._resume_session(self.session)
        self.assertFalse(self.session.sleeping)

    async def test_sdk_voice_reconnect_does_not_retire_or_disconnect_session(self):
        self.session.current_song = song("current")
        self.session.queue = [song("next")]
        self.session.position_seconds = 42
        self.vc.is_connected.return_value = False
        before, after = SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None)
        with self.assertLogs("bot_app.application.music.sessions", level="INFO") as logs:
            await self.service.on_voice_state_update(self.bot.user, before, after)
            task = self.session.voice_recovery_task
            self.assertIsNotNone(task)
            self.service._cleanup_dead_vc(self.session)
            self.assertIs(self.session.vc, self.vc)
            self.vc.disconnect.assert_not_awaited()
            self.vc.is_connected.return_value = True
            await task
        self.assertIs(self.service.sessions["1"], self.session)
        self.assertFalse(self.session.stopped)
        self.assertEqual(self.session.current_song.title, "current")
        self.assertEqual(len(self.session.queue), 1)
        self.assertTrue(any("recovered after" in line for line in logs.output))
        self.vc.disconnect.assert_not_awaited()

    async def test_status_does_not_rekey_a_session_when_voice_client_moves(self):
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        self.vc.channel = other
        self.service._write_music_status(self.session)
        self.assertEqual(self.session.channel_id, 5)
        self.assertIs(self.service.sessions["1"], self.session)
        self.assertEqual(self.runtime.write_music_session.call_args.args[0], "1")

    async def test_external_channel_move_keeps_player_audio_queue_and_controls(self):
        self.session.current_song = current = song("current")
        self.session.position_seconds = 42
        self.session.queue = [song("next")]
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        self.session.volume = 63
        self.session.loop_mode = "queue"
        self.session.panel_message_id = 123
        self.vc.channel = other
        await self.service.on_voice_state_update(self.bot.user,
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=other))
        self.assertEqual(list(self.service.sessions), ["1"])
        self.assertIs(self.service.sessions["1"], self.session)
        self.assertIs(self.session.current_song, current)
        self.assertEqual([item.title for item in self.session.queue], ["next"])
        self.assertEqual((self.session.position_seconds, self.session.volume, self.session.loop_mode), (42, 63, "queue"))
        self.assertEqual((self.session.channel_id, self.session.panel_message_id), (5, 123))
        self.vc.stop.assert_not_called()
        self.vc.disconnect.assert_not_awaited()
        self.ctx.author.voice.channel = other
        await self.service.dispatch("volume", self.ctx, 65)
        self.audio.set_volume.assert_called_with(self.vc, 65)

    async def test_other_channel_commands_cannot_modify_shared_queue_or_playback(self):
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        self.ctx.author.voice.channel = other
        self.session.queue = [song("waiting")]
        for action, args in (("stop", ()), ("skip", ()), ("pause", ()), ("clear", ()),
                             ("volume", (90,)), ("loop", ("one",)), ("play", ("test",))):
            await self.service.dispatch(action, self.ctx, *args)
        self.assertEqual([item.title for item in self.session.queue], ["waiting"])
        self.assertEqual(self.session.loop_mode, "off")
        self.audio.set_volume.assert_not_called()
        self.vc.pause.assert_not_called()
        self.vc.stop.assert_not_called()
        self.vc.disconnect.assert_not_awaited()
        self.assertEqual(list(self.service.sessions), ["1"])

    async def test_stopping_after_a_move_clears_the_servers_only_queue(self):
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        self.vc.channel = other
        self.ctx.author.voice.channel = other
        self.session.queue = [song("waiting")]
        await self.service.dispatch("stop", self.ctx)
        self.vc.stop.assert_called_once()
        self.vc.disconnect.assert_awaited_once()
        self.assertFalse(self.service.sessions)

    async def test_join_before_move_event_preserves_audio_and_player(self):
        self.session.current_song = current = song("current")
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        self.vc.channel = other
        self.ctx.author.voice.channel = other
        await self.service.dispatch("join", self.ctx)
        self.assertIs(self.service.sessions["1"], self.session)
        self.assertIs(self.session.current_song, current)
        self.vc.stop.assert_not_called()
        self.vc.disconnect.assert_not_awaited()
        await self.service.on_voice_state_update(self.bot.user,
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=other))
        self.vc.stop.assert_not_called()

    async def test_delayed_channel_move_event_cannot_bind_a_newer_connection_location(self):
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        latest = SimpleNamespace(id=6, name="latest voice", guild=self.guild, members=[])
        self.session.current_song = song("current")
        self.vc.channel = latest
        await self.service.on_voice_state_update(self.bot.user,
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=other))
        self.assertNotIn("1:5", self.service.sessions)
        self.assertIs(self.service.sessions["1"], self.session)
        self.assertIs(self.session.vc.channel, latest)
        self.vc.stop.assert_not_called()
        self.vc.disconnect.assert_not_awaited()

    async def test_channel_move_round_trip_preserves_one_queue_and_playback_callback(self):
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        self.session.current_song = current = song("current")
        self.session.queue = [song("next")]
        async def move_to(channel):
            before = self.vc.channel
            self.vc.channel = channel
            self.service._spawn(self.service.on_voice_state_update(self.bot.user,
                SimpleNamespace(channel=before), SimpleNamespace(channel=channel)))
        self.vc.move_to = AsyncMock(side_effect=move_to)
        for channel in (other, self.channel, other, self.channel):
            self.ctx.author.voice.channel = channel
            await self.service.dispatch("join", self.ctx)
            await asyncio.sleep(0)
            self.assertIs(self.service.sessions[self.service._channel_key(channel)].vc, self.vc)
            self.assertEqual(list(self.service.sessions), ["1"])
            self.assertIs(self.service.sessions["1"], self.session)
            self.assertIs(self.session.current_song, current)
            self.assertEqual([item.title for item in self.session.queue], ["next"])
        self.vc.stop.assert_not_called()
        self.vc.disconnect.assert_not_awaited()
        self.service.play_next = AsyncMock()
        await self.service._after_playback("1", self.session, current, 0, None)
        self.assertEqual(self.session.history[0]["title"], "current")
        self.service.play_next.assert_awaited_once_with("1")

    async def test_another_channel_cannot_connect_during_guild_voice_recovery(self):
        self.vc.is_connected.return_value = False
        await self.service.on_voice_state_update(self.bot.user,
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[], connect=AsyncMock())
        target = self.service._get_session(other)
        self.assertIsNone(await self.service._ensure_connected(self.ctx, other, target))
        other.connect.assert_not_awaited()
        self.ctx.send.assert_awaited_with("Voice is reconnecting. Please retry in a moment.")

    async def test_failed_join_move_keeps_existing_track_queue_and_location(self):
        self.session.current_song = current = song("current")
        self.session.queue = [song("next")]
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        self.ctx.author.voice.channel = other
        self.vc.move_to = AsyncMock(side_effect=RuntimeError("move rejected"))
        with self.assertLogs("bot_app.application.music.playback", level="WARNING"):
            await self.service.dispatch("join", self.ctx)
        self.assertIs(self.service.sessions["1"], self.session)
        self.assertEqual(self.session.channel_id, 2)
        self.assertIs(self.session.current_song, current)
        self.assertEqual([item.title for item in self.session.queue], ["next"])
        self.vc.stop.assert_not_called()
        self.vc.disconnect.assert_not_awaited()

    async def test_disconnected_server_queue_can_resume_in_another_voice_channel(self):
        self.session.vc = None
        self.session.restored = True
        self.session.queue = [song("saved")]
        self.session.queue[0].resume_at = 73
        self.guild.voice_client = None
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        async def connect(**kwargs):
            self.guild.voice_client = self.vc
            self.vc.channel = other
            return self.vc
        other.connect = AsyncMock(side_effect=connect)
        self.ctx.author.voice.channel = other
        self.service.play_next = AsyncMock()
        await self.service.dispatch("resume", self.ctx)
        await asyncio.sleep(0)
        self.assertEqual(list(self.service.sessions), ["1"])
        self.assertIs(self.service.sessions["1"], self.session)
        self.assertEqual((self.session.channel_id, self.session.queue[0].resume_at), (5, 73))
        self.service.play_next.assert_awaited_once_with("1")

    async def test_paused_channel_move_retains_pause_position_and_server_isolation(self):
        self.session.current_song = current = song("paused")
        self.session.paused_since = 123
        self.session.position_seconds = 73
        self.vc.is_playing.return_value = False
        self.vc.is_paused.return_value = True
        other_guild = ChannelSession(guild_id=8, channel_id=9, queue=[song("other server")])
        self.service.sessions["8"] = other_guild
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        self.vc.channel = other
        await self.service.on_voice_state_update(self.bot.user,
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=other))
        self.assertIs(self.session.current_song, current)
        self.assertEqual((self.session.paused_since, self.service.position(self.session)), (123, 73))
        self.assertIs(self.service.sessions["8"], other_guild)
        self.assertEqual([item.title for item in other_guild.queue], ["other server"])
        self.vc.stop.assert_not_called()
        self.vc.resume.assert_not_called()

    async def test_concurrent_joins_create_only_one_server_queue_and_connection(self):
        self.service.sessions.clear()
        self.guild.voice_client = None
        other = SimpleNamespace(id=5, name="other voice", guild=self.guild, members=[])
        other_ctx = SimpleNamespace(guild=self.guild, channel=self.ctx.channel, send=AsyncMock(),
            author=SimpleNamespace(id=6, voice=SimpleNamespace(channel=other)))
        async def connect(**kwargs):
            await asyncio.sleep(0)
            self.vc.channel = self.channel
            self.guild.voice_client = self.vc
            return self.vc
        async def move_to(channel):
            self.vc.channel = channel
        self.channel.connect = AsyncMock(side_effect=connect)
        other.connect = AsyncMock()
        self.vc.move_to = AsyncMock(side_effect=move_to)
        await asyncio.gather(self.service.dispatch("join", self.ctx), self.service.dispatch("join", other_ctx))
        self.assertEqual(list(self.service.sessions), ["1"])
        self.channel.connect.assert_awaited_once()
        other.connect.assert_not_awaited()
        self.assertIs(self.service.sessions["1"].vc, self.vc)
        self.assertEqual(self.service.sessions["1"].channel_id, 5)

    async def test_failed_voice_recovery_preserves_queue_and_frozen_position(self):
        from bot_app.infrastructure.persistence.queues import QueueRepository
        self.service.queue_repository = QueueRepository(Path(self.tmp.name) / "queues")
        self.session.current_song = song("current")
        self.session.position_seconds = 42
        self.session.queue = [song("next")]
        self.vc.is_connected.return_value = False
        self.vc.is_playing.return_value = False
        with patch("bot_app.application.music.sessions.VOICE_RECOVERY_TIMEOUT_SECONDS", 0):
            with self.assertLogs("bot_app.application.music.sessions", level="INFO") as logs:
                await self.service.on_voice_state_update(self.bot.user,
                    SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
                await self.session.voice_recovery_task
        parked = self.service.sessions["1"]
        self.assertTrue(parked.restored)
        self.assertIsNone(parked.vc)
        self.assertEqual([s.title for s in parked.queue], ["current", "next"])
        self.assertEqual(parked.queue[0].resume_at, 42)
        self.assertEqual(self.service.queue_repository.load()["1"]["tracks"][0]["resume_at"], 42)
        self.assertTrue(any("timed out" in line for line in logs.output))
        self.assertTrue(any("Voice session ending" in line for line in logs.output))

    async def test_stop_cancels_pending_recovery_and_does_not_restore_queue(self):
        self.session.current_song = song("current")
        self.vc.is_connected.return_value = False
        await self.service.on_voice_state_update(self.bot.user,
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        task = self.session.voice_recovery_task
        await self.service._dispose_session("1")
        self.assertTrue(task.cancelled())
        self.assertNotIn("1", self.service.sessions)

    async def test_audio_callback_during_reconnect_keeps_current_track(self):
        current = self.session.current_song = song("current")
        self.vc.is_connected.return_value = False
        await self.service.on_voice_state_update(self.bot.user,
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        await self.service._after_playback("1", self.session, current, self.session.generation, None)
        self.assertIs(self.session.current_song, current)
        self.assertEqual(self.session.history, [])
        self.ctx.send.reset_mock()
        self.assertIsNone(await self.service._ensure_connected(self.ctx, self.channel, self.session))
        self.ctx.send.assert_awaited_once_with("Voice is reconnecting. Please retry in a moment.")

    async def test_recovery_restarts_audio_that_ended_during_handshake(self):
        current = self.session.current_song = song("current")
        self.session.position_seconds = 42
        self.session.queue = [song("next")]
        self.vc.is_connected.return_value = False
        self.vc.is_playing.return_value = False
        self.service.play_next = AsyncMock()
        await self.service.on_voice_state_update(self.bot.user,
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        task = self.session.voice_recovery_task
        self.vc.is_connected.return_value = True
        await task
        await asyncio.sleep(0)
        self.assertEqual([s.title for s in self.session.queue], ["current", "next"])
        self.assertEqual(current.resume_at, 42)
        self.assertIsNone(self.session.current_song)
        self.assertEqual(self.session.playback_id, 1)
        self.service.play_next.assert_awaited_once_with("1")
        # The old player's callback must not consume or delete the replayed track.
        await self.service._after_playback("1", self.session, current, self.session.generation, None, 0)
        self.assertEqual(self.session.history, [])

    async def test_recovered_paused_track_stays_paused_when_audio_restarts(self):
        current = self.session.current_song = song("current")
        self.session.paused_since = 100
        self.vc.is_playing.return_value = False
        self.vc.is_paused.return_value = False
        await self.service._play_local_file(self.vc, self.media.music_dir / "current.mp3", "1", current)
        self.vc.pause.assert_called_once()
        self.assertIsNone(self.session.playing_since)
        self.assertEqual(self.session.paused_since, 100)

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
            await self.service._idle_watch_loop("1")
        parked = self.service.sessions["1"]
        self.assertIsNot(parked, self.session)
        self.assertTrue(parked.restored)
        self.assertIsNone(parked.vc)
        self.assertEqual([s.title for s in parked.queue], ["current", "next"])
        self.assertEqual(parked.queue[0].resume_at, 73)
        self.vc.disconnect.assert_awaited_once()
        self.assertEqual(self.service.queue_repository.load()["1"]["tracks"][0]["resume_at"], 73)
        self.service._persist_session(self.session)
        self.assertEqual(self.service.queue_repository.load()["1"]["tracks"][0]["resume_at"], 73)

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
                await self.service._idle_watch_loop("1")
        self.vc.disconnect.assert_not_awaited()
        self.assertIs(self.service.sessions["1"], self.session)

    async def test_play_cannot_reuse_session_while_disconnect_is_pending(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def disconnect(**kwargs):
            started.set()
            await finish.wait()
        self.vc.disconnect = disconnect
        self.session.queue = [song("saved")]
        task = asyncio.create_task(self.service._dispose_session("1", preserve=True))
        await started.wait()
        try:
            with self.assertRaisesRegex(ValueError, "Disconnecting"):
                self.service._get_session(self.channel)
        finally:
            finish.set()
            await task
        self.assertTrue(self.service.sessions["1"].restored)
        self.assertFalse(self.service.sessions["1"].retiring)

    async def test_relative_seek_uses_current_position(self):
        self.session.current_song = song("current")
        self.session.position_seconds = 45
        self.service._seek_session = AsyncMock()
        await self.service.dispatch("seek", self.ctx, "+30")
        self.service._seek_session.assert_awaited_with("1", self.session, 75)
        await self.service.dispatch("seek", self.ctx, "-90")
        self.service._seek_session.assert_awaited_with("1", self.session, 0)

    async def test_web_resume_of_saved_queue_connects_only_when_someone_is_present(self):
        self.session.vc = None
        self.session.restored = True
        self.session.queue = [song("saved")]
        self.bot.get_channel = lambda _: self.channel
        with self.assertRaisesRegex(ValueError, "Join the last"):
            await self.service.execute_control({"action":"resume", "session_id":"1"})
        self.channel.members = [SimpleNamespace(bot=False)]
        self.service.play_next = AsyncMock()
        await self.service.execute_control({"action":"resume", "session_id":"1"})
        await asyncio.sleep(0)
        self.assertIs(self.session.vc, self.vc)
        self.assertFalse(self.session.restored)
        self.service.play_next.assert_awaited_once_with("1")


class MediaTests(unittest.TestCase):
    def test_local_file_scope_and_url_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test.mp3").write_bytes(b"audio")
            media = MediaSource("ffmpeg", root)
            self.assertEqual(media._resolve_local_music("test"), (root / "test.mp3").resolve())
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
