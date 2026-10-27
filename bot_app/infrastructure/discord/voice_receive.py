"""Receive encrypted Opus, decrypt DAVE once, then decode to in-memory PCM.

Using an Opus sink keeps the unmodified receive extension from trying to decode
DAVE ciphertext (including its FEC path). No SDK monkey patches are required.
"""
import audioop
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import discord
from discord.ext import voice_recv
from davey import MediaType


class SpeechVoiceClient(voice_recv.VoiceRecvClient):
    def is_paused(self):
        return getattr(self, "_reply_preserves_pause", False) or super().is_paused()

    def is_playing(self):
        return False if getattr(self, "_reply_preserves_pause", False) else super().is_playing()

    def stop(self):
        # Music calls stop() when skipping/seeking. Keep the microphone receiver.
        self.stop_playing()

    def cleanup(self):
        self.stop_listening()
        super().cleanup()

    async def disconnect(self, *, force=False):
        self.stop_listening()
        await super().disconnect(force=force)


@dataclass
class Speaker:
    decoder: object = field(default_factory=discord.opus.Decoder)
    pre: deque = field(default_factory=lambda: deque(maxlen=10))
    frames: list = field(default_factory=list)
    size: int = 0
    voiced: int = 0
    last_voice: float = 0
    last_packet: float = 0
    decoded: int = 0
    peak_rms: int = 0
    started_at: float = 0
    last_sequence: object = None
    missing_packets: int = 0
    reply_overlap: bool = False


class SpeechSink(voice_recv.AudioSink):
    MAX_BYTES = 48000 * 2 * 2 * 12
    MIN_VOICED = 48000 * 2 * 2 * 0.25

    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self.channel_id = connection.channel.id
        self.speakers = {}
        self.last_segments = {}
        self.lock = threading.Lock()
        self.closed = False
        self.reply_active = False
        self.packets = self.decoded = self.errors = 0

    def wants_opus(self):
        return True

    def write(self, user, data):
        if not user or user.bot or not data.opus:
            return
        with self.lock:
            if self.closed or self.connection.channel.id != self.channel_id:
                return
            self.packets += 1
            now = time.monotonic()
            state = self.speakers.get(user.id)
            if state is None:
                if len(self.speakers) >= 16:
                    return
                state = self.speakers[user.id] = Speaker()
            payload = bytes(data.opus)
            sequence = getattr(getattr(data, 'packet', None), 'sequence', None)
            if isinstance(sequence, int):
                if state.last_sequence is not None:
                    gap = (sequence - state.last_sequence) & 65535
                    if 1 < gap < 2000:
                        state.missing_packets += gap - 1
                state.last_sequence = sequence
            session = self.connection._connection.dave_session
            try:
                if payload != b"\xf8\xff\xfe" and session is not None:
                    if not session.ready:
                        return
                    payload = session.decrypt(user.id, MediaType.audio, payload)
                pcm = state.decoder.decode(payload, fec=False)
            except Exception:
                self.errors += 1
                return  # Transition or lost packets must never kill reception.
            self.decoded += 1
            state.last_packet = now
            if self.reply_active:
                state.reply_overlap = True
            level = audioop.rms(pcm, 2)
            state.decoded += 1
            state.peak_rms = max(state.peak_rms, level)
            loud = level >= 180
            if loud:
                if not state.frames:
                    state.started_at = now
                    state.frames.extend(state.pre)
                    state.size = sum(map(len, state.pre))
                    state.pre.clear()
                state.last_voice = now
                state.voiced += len(pcm)
            if state.frames or loud:
                if state.size < self.MAX_BYTES:
                    state.frames.append(pcm)
                    state.size += len(pcm)
            else:
                state.pre.append(pcm)

    def take_ready(self, now):
        ready = []
        with self.lock:
            if self.closed:
                return ready
            for user_id, state in list(self.speakers.items()):
                if state.frames and (now - state.last_voice >= 0.65 or state.size >= self.MAX_BYTES):
                    if state.voiced >= self.MIN_VOICED:
                        self.last_segments[user_id] = {'audio_seconds': round(state.size / 192000, 2),
                            'wall_seconds': round(max(0, state.last_voice - state.started_at) + 0.02, 2)}
                        ready.append((user_id, b"".join(state.frames), now, state.reply_overlap))
                    state.frames.clear()
                    state.size = state.voiced = 0
                    state.reply_overlap = False
                if now - state.last_packet > 30:
                    self.speakers.pop(user_id, None)
        return ready

    def activity(self, user_id):
        with self.lock:
            state = self.speakers.get(user_id)
            return {"frames": state.decoded, "peak_rms": state.peak_rms,
                    "buffer_seconds": round(state.size / 192000, 2), "missing_packets": state.missing_packets,
                    "last_segment": self.last_segments.get(user_id, {})} if state else {
                        "frames": 0, "peak_rms": 0, "buffer_seconds": 0}

    def set_reply_active(self, active):
        with self.lock:
            self.reply_active = active
            if active:
                for state in self.speakers.values():
                    if state.frames:
                        state.reply_overlap = True

    def cleanup(self):
        with self.lock:
            self.closed = True
            self.speakers.clear()
            self.last_segments.clear()
