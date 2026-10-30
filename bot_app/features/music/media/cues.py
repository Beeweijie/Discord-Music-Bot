"""Short, pre-generated replies mixed with music; no network calls at runtime."""
import asyncio
import audioop
import io
import threading
import wave
from pathlib import Path

import discord


class ReplyMixer(discord.PCMVolumeTransformer):
    FRAME_BYTES = 3840

    def __init__(self, original, *, volume=1):
        super().__init__(original, volume=volume)
        self.freeze_music = False
        self._reply = b""
        self._offset = 0
        self._done = None
        self._lock = threading.Lock()

    def add_reply(self, pcm, done):
        self.cancel_reply()
        with self._lock:
            self._reply, self._offset, self._done = pcm, 0, done

    def cancel_reply(self):
        with self._lock:
            done = self._done
            self._reply, self._offset, self._done = b"", 0, None
        if done:
            done()

    def read(self):
        music = b"\0" * self.FRAME_BYTES if self.freeze_music else super().read()
        with self._lock:
            if not self._reply:
                return music
            voice = self._reply[self._offset:self._offset + self.FRAME_BYTES]
            self._offset += self.FRAME_BYTES
            done = None
            if self._offset >= len(self._reply):
                done, self._done = self._done, None
                self._reply, self._offset = b"", 0
        music = music.ljust(self.FRAME_BYTES, b"\0")
        result = audioop.add(audioop.mul(music, 2, 0.25), voice.ljust(self.FRAME_BYTES, b"\0"), 2)
        if done:
            done()
        return result

    def cleanup(self):
        self.cancel_reply()
        super().cleanup()


class VoiceReplies:
    def __init__(self):
        self.clips = {}
        self.active = {}
        for name in ("wake", "ok", "unclear", "timeout", "choices"):
            clip = Path(__file__).with_name("replies") / (name + ".wav")
            if clip.is_file():
                data = clip.read_bytes()
            else:
                from bot_app.features.music.media.voice_assets import clip_bytes
                data = clip_bytes(name)
            with wave.open(io.BytesIO(data), "rb") as source:
                if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (48000, 2, 2):
                    raise ValueError("Voice replies must be Discord PCM")
                self.clips[name] = source.readframes(source.getnframes())

    def stop(self, connection):
        source = self.active.get(id(connection))
        if source is None or connection.source is not source:
            return False
        if isinstance(source, ReplyMixer):
            source.cancel_reply()
        else:
            connection.stop()
        return True

    async def say(self, connection, session, name, on_speaking=None):
        pcm = self.clips[name]
        # Match the music service's control -> play lock order. An in-progress
        # download may hold play_lock; a reply must never steal that player.
        async with session.control_lock:
            try:
                await asyncio.wait_for(session.play_lock.acquire(), timeout=3)
            except asyncio.TimeoutError:
                return False
            try:
                if not connection.is_connected() or session.stopped or session.retiring:
                    return False
                loop = asyncio.get_running_loop()
                finished = asyncio.Event()
                def done(*_):
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(finished.set)
                source = connection.source
                paused = connection.is_paused()
                if isinstance(source, ReplyMixer) and (connection.is_playing() or paused):
                    self.active[id(connection)] = source
                    if on_speaking:
                        on_speaking(True)
                    source.add_reply(pcm, done)
                    if paused:
                        source.freeze_music = True
                        connection._reply_preserves_pause = True
                        discord.VoiceClient.resume(connection)
                    try:
                        await asyncio.wait_for(finished.wait(), timeout=len(pcm) / 192000 + 2)
                        # The callback occurs when the final frame is read.
                        await asyncio.sleep(0.04)
                    finally:
                        if paused:
                            if connection.source is source:
                                discord.VoiceClient.pause(connection)
                            source.freeze_music = False
                            connection._reply_preserves_pause = False
                        source.cancel_reply()
                elif not connection.is_playing() and not paused:
                    cue = discord.PCMAudio(io.BytesIO(pcm))
                    self.active[id(connection)] = cue
                    if on_speaking:
                        on_speaking(True)
                    connection.play(cue, after=done)
                    try:
                        await asyncio.wait_for(finished.wait(), timeout=len(pcm) / 192000 + 2)
                    finally:
                        if connection.source is cue:
                            connection.stop()
                else:
                    return False
                return True
            finally:
                self.active.pop(id(connection), None)
                if on_speaking:
                    on_speaking(False)
                session.play_lock.release()
