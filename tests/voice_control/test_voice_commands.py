import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot_app.features.voice_control.grammar import WakeGate, SpeechResult
from bot_app.features.voice_control.discord import VoiceCommands


class GrammarTests(unittest.TestCase):
    def setUp(self):
        self.gate = WakeGate()

    def test_wake_and_controls(self):
        for text, action in [("喵酱喵酱，下一首歌。", "skip"),
                             ("妙酱，妙酱，暂停播放", "pause"),
                             ("苗疆苗疆继续播放", "resume"),
                             ("喵醬喵醬，下一首", "skip")]:
            self.assertEqual(self.gate.accept(1, text, 10).action, action)

    def test_playqueue_is_a_control_not_a_song_search(self):
        for text in ['播放歌单', '播放列表吧', '播放队列']:
            self.assertEqual(self.gate.accept(1, '喵酱喵酱' + text, 10).action, 'playqueue')

    def test_song_query_and_english_spaces(self):
        result = self.gate.accept(1, "喵酱喵酱，播放 周杰伦 的 晴天", 10)
        self.assertEqual((result.action, result.query), ("play", "周杰伦的晴天"))
        self.assertEqual(self.gate.accept(1, "喵酱喵酱播放 Taylor Swift Love Story", 10).query,
                         "Taylor Swift Love Story")
        command = self.gate.accept(1, '喵酱喵酱加入雨爱吧', 10)
        self.assertEqual((command.action, command.query), ('enqueue', '雨爱吧'))

    def test_conversation_single_name_and_unknown_commands_do_not_trigger(self):
        for text in ["下一首歌", "播放晴天", "喵酱下一首", "我说喵酱喵酱下一首歌",
                     "今天很好听"]:
            self.assertIsNone(self.gate.accept(1, text, 10), text)

    def test_unknown_command_after_wake_acknowledges_without_dispatching(self):
        for text in ["喵酱喵酱播放", "喵酱喵酱下一首歌很好听", "喵酱喵酱删除所有歌曲"]:
            self.assertEqual(self.gate.accept(1, text, 10).action, "unclear")

    def test_name_split_between_segments_and_repeated_names(self):
        self.assertIsNone(self.gate.accept(1, "喵酱", 10))
        self.assertEqual(self.gate.accept(1, "喵酱，下一首歌", 11).action, "skip")
        self.assertEqual(self.gate.accept(1, "喵酱喵酱喵酱，下一首歌", 12).action, "skip")
        self.gate.accept(1, "喵酱", 20)
        self.assertIsNone(self.gate.accept(1, "喵酱下一首歌", 24))

    def test_split_wake_is_separate_for_each_speaker(self):
        self.gate.accept(1, "喵酱", 10)
        self.assertIsNone(self.gate.accept(2, "喵酱下一首歌", 11))

    def test_phonetic_wake_works_even_when_asr_misspells_the_name(self):
        self.assertEqual(self.gate.accept(1, "鸟叫鸟叫，下一首歌", 10, heard_wake=True).action, "skip")
        self.assertIsNone(WakeGate().accept(1, "鸟叫鸟叫，下一首歌", 10))
        self.assertEqual(self.gate.accept(1, "", 11, heard_wake=True).action, "wake")
        self.assertEqual(self.gate.accept(1, "播放晴天", 12).query, "晴天")

    def test_actual_microphone_transcripts_with_clipped_first_syllable(self):
        for text in ["将喵将下一首歌", "将喵降下一首歌吧"]:
            self.assertEqual(self.gate.accept(1, text, 10).action, "skip")
            self.assertEqual(self.gate.last_reason, "clipped_wake")
        self.assertEqual(self.gate.accept(1, "将喵将播放周杰伦的晴天", 10).query, "周杰伦的晴天")
        self.assertEqual(self.gate.accept(1, "将喵降暂停播放", 10).action, "pause")

    def test_clipped_name_requires_complete_command_and_three_syllables(self):
        for text in ["喵将下一首歌", "将下一首歌", "将喵将下一首歌很好听",
                     "我说将喵将下一首歌", "将喵降删除歌曲"]:
            self.assertIsNone(self.gate.accept(1, text, 10), text)
        self.assertEqual(self.gate.accept(1, "将喵将", 10).action, "wake")

    def test_polite_command_and_unrelated_segment_does_not_consume_arm(self):
        self.gate.accept(1, "喵酱喵酱", 10)
        self.assertIsNone(self.gate.accept(1, "嗯", 11))
        self.assertEqual(self.gate.accept(1, "请帮我下一首歌吧", 12).action, "skip")

    def test_split_wake_is_one_shot_timed_and_per_speaker(self):
        self.assertEqual(self.gate.accept(1, "喵酱喵酱", 10).action, "wake")
        self.assertIsNone(self.gate.accept(2, "下一首歌", 11))
        self.assertEqual(self.gate.accept(1, "下一首歌", 12).action, "skip")
        self.assertIsNone(self.gate.accept(1, "下一首歌", 13))
        self.gate.accept(1, "喵酱喵酱", 15)
        self.assertIsNone(self.gate.accept(1, "下一首歌", 24))


class VoiceDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel = Mock(id=10)
        self.channel.send = AsyncMock()
        self.channel.permissions_for.return_value.view_channel = True
        self.member = SimpleNamespace(id=2, bot=False, voice=SimpleNamespace(channel=self.channel, suppress=False))
        self.guild = SimpleNamespace(id=1, get_member=lambda uid: self.member if uid == 2 else None)
        self.vc = Mock(guild=self.guild, channel=self.channel)
        self.vc.is_connected.return_value = True
        self.guild.voice_client = self.vc
        self.sink = SimpleNamespace(closed=False, channel_id=10, cleanup=Mock())
        self.service = SimpleNamespace(sessions={"1": SimpleNamespace(last_text_channel_id=20)}, dispatch=AsyncMock())
        self.bot = SimpleNamespace(get_channel=lambda _: self.channel,
            get_cog=lambda _: SimpleNamespace(service=self.service))
        self.cog = VoiceCommands(self.bot)
        self.gate = WakeGate()
        self.cog.active[1] = (self.vc, self.sink, self.gate)
        self.addAsyncCleanup(self.cog.cog_unload)

    async def test_voice_play_reuses_normal_dispatch_and_author(self):
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "喵酱喵酱播放晴天")
        action, ctx, query = self.service.dispatch.await_args.args
        self.assertEqual((action, query), ("playskip", "晴天"))
        self.assertIs(ctx.author, self.member)
        self.assertIs(ctx.guild, self.guild)

    async def test_missing_native_codec_reports_error_before_starting_receiver(self):
        if SpeechSink is None:
            self.skipTest('optional voice dependencies not installed')
        import discord
        self.cog.enabled = True
        self.cog.publish = Mock()
        with patch('discord.opus.Decoder', side_effect=discord.opus.OpusNotLoaded()), self.assertLogs(
                'bot_app.features.voice_control.discord', level='ERROR'):
            await self.cog.cog_load()
        self.cog.publish.assert_called_once_with('error')
        self.assertIsNone(self.cog.recognizer)
        self.assertEqual(self.cog.tasks, [])

    async def test_music_association_dispatches_selected_url_without_second_search(self):
        from bot_app.features.music.phonetics import QueryGuess
        self.cog.music_search = Mock()
        session = self.service.sessions['1']
        session.queue, session.history = [], []
        self.service.library_repository = None
        self.service.media = SimpleNamespace(_search_youtube_results=Mock())
        selected = {'title': '杨丞琳 - 雨爱 Official Audio', 'url': 'https://www.youtube.com/watch?v=rainlove001'}
        self.service.run_media = AsyncMock(return_value=(QueryGuess('与爱', '雨爱'), [selected], selected))
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '喵酱喵酱播放与爱')
        self.assertEqual(self.service.dispatch.await_args.args[0], 'playskip')
        self.assertEqual(self.service.dispatch.await_args.args[2], selected['url'])

    async def test_song_candidate_selection_preserves_enqueue_intent(self):
        item = {'title': '雨爱', 'url': 'https://www.youtube.com/watch?v=rainlove001'}
        self.cog.song_choices[(1, 2)] = {'sink': self.sink, 'expires': time.monotonic() + 30,
            'action': 'enqueue', 'items': [item]}
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '第一个')
        self.assertEqual(self.service.dispatch.await_args.args[0], 'enqueue')
        self.assertEqual(self.service.dispatch.await_args.args[2], item['url'])

    async def test_moved_disconnected_bot_and_replaced_connection_cannot_control(self):
        for mode in ["moved", "disconnected", "bot", "replaced", "closed", "no-view"]:
            with self.subTest(mode=mode):
                self.member.voice.channel = self.channel
                self.member.bot = False
                self.vc.is_connected.return_value = True
                self.guild.voice_client = self.vc
                self.sink.closed = False
                self.channel.permissions_for.return_value.view_channel = True
                if mode == "moved": self.member.voice.channel = SimpleNamespace(id=99)
                if mode == "disconnected": self.vc.is_connected.return_value = False
                if mode == "bot": self.member.bot = True
                if mode == "replaced": self.guild.voice_client = Mock()
                if mode == "closed": self.sink.closed = True
                if mode == "no-view": self.channel.permissions_for.return_value.view_channel = False
                await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "喵酱喵酱下一首歌")
                self.service.dispatch.assert_not_awaited()

    async def test_skip_debounced(self):
        for _ in range(2):
            await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "喵酱喵酱下一首歌")
        self.service.dispatch.assert_awaited_once()

    async def test_pending_result_rechecks_live_membership(self):
        def recognize(_, key):
            self.member.voice.channel = SimpleNamespace(id=99)
            return SpeechResult("喵酱喵酱下一首歌")
        self.cog.recognizer = SimpleNamespace(recognize=recognize)
        self.cog.queue.put_nowait((self.vc, self.sink, self.gate, 2, b"audio", time.monotonic(), False))
        consumer = asyncio.create_task(self.cog.consume())
        await asyncio.wait_for(self.cog.queue.join(), 2)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        self.service.dispatch.assert_not_awaited()

    async def test_stale_audio_not_transcribed(self):
        self.cog.recognizer = Mock()
        self.cog.queue.put_nowait((self.vc, self.sink, self.gate, 2, b"audio", time.monotonic() - 10, False))
        consumer = asyncio.create_task(self.cog.consume())
        await asyncio.wait_for(self.cog.queue.join(), 2)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        self.cog.recognizer.recognize.assert_not_called()

    async def test_wake_ack_does_not_debounce_followup_command(self):
        self.cog.say = AsyncMock()
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "喵酱喵酱")
        self.cog.say.assert_awaited_with(self.vc, self.sink, "wake")
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "下一首歌")
        self.service.dispatch.assert_awaited_once()
        self.assertEqual(self.service.dispatch.await_args.args[0], "skip")
        self.cog.say.assert_awaited_with(self.vc, self.sink, "ok")

    async def test_wake_does_not_discard_followup_recorded_while_asr_was_busy(self):
        stamp = time.monotonic() - 1
        self.cog.schedule_text(self.vc, self.sink, self.gate, 2, SpeechResult('喵酱喵酱'), stamp=stamp)
        await asyncio.gather(*list(self.cog.command_jobs))
        self.assertEqual(self.cog.ignore_before[(1, 2)], stamp)
        self.cog.schedule_text(self.vc, self.sink, self.gate, 2, SpeechResult('下一首歌'), stamp=stamp + 0.5)
        await asyncio.gather(*list(self.cog.command_jobs))
        self.assertEqual(self.service.dispatch.await_args.args[0], 'skip')

    async def test_unclear_does_not_extend_window_and_only_replies_once(self):
        self.cog.say = AsyncMock()
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '喵酱喵酱')
        expiry = self.gate.armed[2]
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '听错了')
        self.assertEqual(self.gate.armed[2], expiry)
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '还是不对')
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '又不对')
        self.assertNotIn(2, self.gate.armed)
        self.assertEqual([call.args[2] for call in self.cog.say.await_args_list], ['wake', 'unclear'])
        self.service.dispatch.assert_not_awaited()

    async def test_quiet_clears_armed_and_choices_without_music_control(self):
        for phrase in ['闭嘴', '取消', '喵酱喵酱别说了', '安静吧']:
            self.gate.arm(2, time.monotonic())
            self.cog.song_choices[(1, 2)] = {'sink': self.sink, 'expires': time.monotonic() + 30, 'items': []}
            self.cog.replies = Mock()
            await self.cog.handle_text(self.vc, self.sink, self.gate, 2, phrase, overlap=True)
            self.assertNotIn(2, self.gate.armed)
            self.assertNotIn((1, 2), self.cog.song_choices)
            self.cog.replies.stop.assert_called_with(self.vc)
        self.service.dispatch.assert_not_awaited()

    async def test_wake_exits_candidate_menu(self):
        self.cog.song_choices[(1, 2)] = {'sink': self.sink, 'expires': time.monotonic() + 30, 'items': []}
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '喵酱喵酱')
        self.assertNotIn((1, 2), self.cog.song_choices)
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '下一首歌')
        self.assertEqual(self.service.dispatch.await_args.args[0], 'skip')

    async def test_overlap_keeps_real_followup_and_ignores_reply_echo(self):
        self.cog.say = AsyncMock()
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '喵酱喵酱')
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '我在喵', overlap=True)
        self.assertIn(2, self.gate.armed)
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, '我在喵请下一首歌', overlap=True)
        self.assertEqual(self.service.dispatch.await_args.args[0], 'skip')

    async def test_slow_search_can_be_interrupted_without_dispatching_late_result(self):
        from bot_app.features.music.phonetics import QueryGuess
        self.cog.music_search = Mock()
        self.service.sessions['1'].queue, self.service.sessions['1'].history = [], []
        self.service.library_repository = None
        self.service.media = SimpleNamespace(_search_youtube_results=Mock())
        entered = asyncio.Event()
        async def slow(*args):
            entered.set()
            await asyncio.Event().wait()
        self.service.run_media = slow
        self.cog.schedule_text(self.vc, self.sink, self.gate, 2, SpeechResult('喵酱喵酱播放雨爱'))
        await asyncio.wait_for(entered.wait(), 1)
        old = self.cog.user_jobs[(1, 2)]
        # Even a follow-up task that has not started waiting on the search yet
        # must not hide the old search from cancellation.
        self.gate.arm(2, time.monotonic())
        self.cog.schedule_text(self.vc, self.sink, self.gate, 2, SpeechResult('听错了'))
        self.cog.schedule_text(self.vc, self.sink, self.gate, 2, SpeechResult('闭嘴'))
        await asyncio.wait_for(asyncio.gather(*list(self.cog.command_jobs), return_exceptions=True), 1)
        self.assertTrue(old.cancelled())
        self.service.dispatch.assert_not_awaited()
        self.assertNotIn(2, self.gate.armed)

    async def test_leaving_during_spoken_reply_prevents_command(self):
        async def say(*_):
            self.member.voice.channel = SimpleNamespace(id=99)
        self.cog.say = say
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "喵酱喵酱下一首歌")
        self.service.dispatch.assert_not_awaited()

    async def test_different_actions_are_not_silently_debounced(self):
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "喵酱喵酱暂停")
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "喵酱喵酱继续")
        self.assertEqual(self.service.dispatch.await_count, 2)

    async def test_diagnostic_only_reports_opted_in_speaker_and_expires(self):
        ctx = SimpleNamespace(send=AsyncMock())
        self.sink.activity = lambda uid: {"peak_rms": 1000}
        test = {"ctx": ctx, "expires": time.monotonic() + 30, "remaining": 3}
        self.cog.test_users[(1, 2)] = test
        await self.cog.report_test(self.vc, self.sink, 3, SpeechResult("other person's speech"))
        ctx.send.assert_not_awaited()
        await self.cog.report_test(self.vc, self.sink, 2, SpeechResult("喵酱喵酱下一首歌", True))
        ctx.send.assert_awaited_once()
        self.assertIn("喵酱喵酱下一首歌", ctx.send.await_args.args[0])
        self.assertEqual(test["remaining"], 2)
        test["expires"] = time.monotonic() - 1
        await self.cog.report_test(self.vc, self.sink, 2, SpeechResult("expired"))
        self.assertEqual(ctx.send.await_count, 1)

    async def test_ordinary_transcription_is_not_reported_without_test_mode(self):
        ctx = SimpleNamespace(send=AsyncMock())
        await self.cog.report_test(self.vc, self.sink, 2, SpeechResult("ordinary speech"))
        ctx.send.assert_not_awaited()

    async def test_disable_invalidates_pending_result(self):
        self.cog.stop_guild(1)
        await self.cog.handle_text(self.vc, self.sink, self.gate, 2, "喵酱喵酱下一首歌")
        self.service.dispatch.assert_not_awaited()


try:
    from bot_app.host.discord.voice_receive import SpeechSink, SpeechVoiceClient
except ImportError:
    SpeechSink = SpeechVoiceClient = None


@unittest.skipIf(SpeechSink is None, "optional voice dependencies not installed")
class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock(channel=SimpleNamespace(id=10))
        self.sink = SpeechSink(self.connection)
        self.addCleanup(self.sink.cleanup)
        self.user = SimpleNamespace(id=2, bot=False)

    def test_dave_decrypted_once_before_opus_decoder(self):
        self.connection._connection.dave_session.decrypt.return_value = b"plain-opus"
        decoder = Mock()
        decoder.decode.return_value = b"\x00\x10" * 1920
        with patch("bot_app.host.discord.voice_receive.discord.opus.Decoder", return_value=decoder):
            from bot_app.host.discord.voice_receive import Speaker
            # Dataclass factory captured the original decoder; replace the state.
            state = Speaker(decoder=decoder)
            self.sink.speakers[2] = state
            self.sink.write(self.user, SimpleNamespace(opus=b"encrypted"))
        self.connection._connection.dave_session.decrypt.assert_called_once()
        decoder.decode.assert_called_once_with(b"plain-opus", fec=False)
        self.assertEqual(self.sink.decoded, 1)

    def test_crypto_transition_drops_frame_and_keeps_sink_alive(self):
        from bot_app.host.discord.voice_receive import Speaker
        self.sink.speakers[2] = Speaker(decoder=Mock())
        self.connection._connection.dave_session.decrypt.side_effect = RuntimeError("epoch changing")
        self.sink.write(self.user, SimpleNamespace(opus=b"encrypted"))
        self.assertEqual(self.sink.errors, 1)
        self.assertFalse(self.sink.closed)

    def test_spoken_reply_preserves_interrupt_audio_and_marks_overlap(self):
        from bot_app.host.discord.voice_receive import Speaker
        decoder = Mock()
        decoder.decode.return_value = b'\x00\x10' * 1920
        state = Speaker(decoder=decoder, frames=[b'old'], size=3, voiced=3)
        self.sink.speakers[2] = state
        self.sink.set_reply_active(True)
        self.sink.write(self.user, SimpleNamespace(opus=b'encrypted'))
        decoder.decode.assert_called_once()
        self.assertTrue(state.frames)
        self.assertTrue(state.reply_overlap)
        self.sink.set_reply_active(False)
        self.sink.write(self.user, SimpleNamespace(opus=b'encrypted'))
        self.assertTrue(state.frames)

    def test_skip_stops_playback_but_keeps_listener(self):
        vc = Mock()
        SpeechVoiceClient.stop(vc)
        vc.stop_playing.assert_called_once()
        vc.stop_listening.assert_not_called()

    def test_cleanup_always_stops_listener(self):
        vc = object.__new__(SpeechVoiceClient)
        vc.stop_listening = Mock()
        with patch("discord.ext.voice_recv.VoiceRecvClient.cleanup"):
            SpeechVoiceClient.cleanup(vc)
        vc.stop_listening.assert_called_once()

    def test_silence_flushes_each_speaker_separately_and_caps_buffers(self):
        from bot_app.host.discord.voice_receive import Speaker
        for uid in [2, 3]:
            self.sink.speakers[uid] = Speaker(decoder=Mock(), frames=[b"a" * 60000],
                size=60000, voiced=60000, last_voice=10, last_packet=10)
        ready = self.sink.take_ready(11)
        self.assertEqual([row[0] for row in ready], [2, 3])
        self.assertEqual(self.sink.take_ready(12), [])
        self.sink.cleanup()
        self.assertEqual(self.sink.take_ready(13), [])
