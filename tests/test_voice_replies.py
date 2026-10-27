import asyncio
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import discord

from bot_app.infrastructure.media.voice_replies import ReplyMixer, VoiceReplies


class MusicSource(discord.AudioSource):
    def __init__(self):
        self.reads = 0
        self.cleaned = False

    def read(self):
        self.reads += 1
        return struct.pack('<h', 1000) * 1920

    def is_opus(self):
        return False

    def cleanup(self):
        self.cleaned = True


class MixerTests(unittest.TestCase):
    def test_reply_ducks_music_then_restores_live_volume(self):
        music = MusicSource()
        source = ReplyMixer(music, volume=0.5)
        self.addCleanup(source.cleanup)
        done = Mock()
        source.add_reply(struct.pack('<h', 2000) * 1920, done)
        self.assertEqual(struct.unpack('<h', source.read()[:2])[0], 2125)
        done.assert_called_once()
        source.volume = 0.8
        self.assertEqual(struct.unpack('<h', source.read()[:2])[0], 800)
        self.assertEqual(music.reads, 2)

    def test_paused_reply_does_not_consume_music_frames(self):
        music = MusicSource()
        source = ReplyMixer(music)
        self.addCleanup(source.cleanup)
        source.freeze_music = True
        source.add_reply(struct.pack('<h', 2000) * 1920, Mock())
        self.assertEqual(struct.unpack('<h', source.read()[:2])[0], 2000)
        self.assertEqual(music.reads, 0)

    def test_cleanup_completes_pending_reply_once(self):
        music = MusicSource()
        source = ReplyMixer(music)
        done = Mock()
        source.add_reply(b'\0' * 3840, done)
        source.cleanup()
        source.cancel_reply()
        done.assert_called_once()
        self.assertTrue(music.cleaned)


class Player:
    def __init__(self, paused=False):
        self.paused = paused

    def resume(self):
        self.paused = False

    def pause(self):
        self.paused = True


class Connection:
    def __init__(self, source, paused=False):
        self.source = source
        self._player = Player(paused)
        self._reply_preserves_pause = False

    def is_connected(self):
        return True

    def is_paused(self):
        return self._reply_preserves_pause or self._player.paused

    def is_playing(self):
        return not self._reply_preserves_pause and not self._player.paused


class PlaybackReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_spoken_reply_preserves_paused_position_and_lock(self):
        music = MusicSource()
        mixer = ReplyMixer(music)
        self.addCleanup(mixer.cleanup)
        connection = Connection(mixer, paused=True)
        session = SimpleNamespace(control_lock=asyncio.Lock(), play_lock=asyncio.Lock(), stopped=False, retiring=False)
        replies = object.__new__(VoiceReplies)
        replies.active = {}
        replies.clips = {'wake': b'\0\x10' * 3840}
        speaking = Mock()
        task = asyncio.create_task(replies.say(connection, session, 'wake', on_speaking=speaking))
        for _ in range(100):
            if mixer._reply:
                break
            await asyncio.sleep(0)
        self.assertTrue(connection.is_paused())
        self.assertFalse(connection._player.paused)
        self.assertTrue(session.control_lock.locked())
        self.assertTrue(session.play_lock.locked())
        mixer.read()
        mixer.read()
        self.assertTrue(await asyncio.wait_for(task, 1))
        self.assertTrue(connection.is_paused())
        self.assertTrue(connection._player.paused)
        self.assertFalse(mixer.freeze_music)
        self.assertEqual(music.reads, 0)
        self.assertFalse(session.control_lock.locked())
        self.assertFalse(session.play_lock.locked())
        self.assertEqual([call.args[0] for call in speaking.call_args_list], [True, False])

    async def test_static_reply_assets_are_valid_and_short(self):
        replies = VoiceReplies()
        self.assertEqual(set(replies.clips), {'wake', 'ok', 'unclear', 'timeout', 'choices'})
        for data in replies.clips.values():
            self.assertGreater(len(data), 19200)
            self.assertLess(len(data), 192000 * 5)

    async def test_release_embedded_replies_match_source_audio(self):
        original = VoiceReplies()
        with patch('bot_app.infrastructure.media.voice_replies.Path.is_file', return_value=False):
            packaged = VoiceReplies()
        self.assertEqual(packaged.clips, original.clips)

    async def test_stop_cancels_only_reply_overlay_and_preserves_music(self):
        music = MusicSource()
        mixer = ReplyMixer(music)
        self.addCleanup(mixer.cleanup)
        replies = VoiceReplies()
        connection = Connection(mixer)
        connection.stop = Mock()
        done = Mock()
        mixer.add_reply(b'\0' * 3840, done)
        replies.active[id(connection)] = mixer
        self.assertTrue(replies.stop(connection))
        done.assert_called_once()
        connection.stop.assert_not_called()
        self.assertEqual(struct.unpack('<h', mixer.read()[:2])[0], 1000)
        self.assertFalse(music.cleaned)
        replies.active.clear()
        self.assertFalse(replies.stop(connection))
