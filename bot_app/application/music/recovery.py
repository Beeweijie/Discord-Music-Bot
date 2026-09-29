"""Event-loop snapshots and explicit playback resumption after restart."""
import asyncio
import logging
import time
from bot_app.application.music.models import ChannelSession, Song
from bot_app.domain.music.library import TRACK_FIELDS, validate_track

logger = logging.getLogger(__name__)


class RecoveryOperations:
    def position(self, session):
        elapsed = time.monotonic() - session.playing_since if session.playing_since is not None else 0
        value = max(0, session.position_seconds + elapsed)
        duration = session.current_song.duration if session.current_song else None
        return min(value, duration) if duration else value

    def _pause_session(self, session):
        session.position_seconds = self.position(session)
        session.playing_since = None
        session.vc.pause()
        session.paused_since = time.monotonic()
        self._write_music_status(session)

    def _resume_session(self, session):
        session.sleeping = False
        session.paused_since = None
        session.vc.resume()
        session.playing_since = time.monotonic()
        self._write_music_status(session)

    def _persist_session(self, session):
        if self.queue_repository is None or self.closing or not session.guild_id or not session.channel_id:
            return
        key = self._session_status_key(session)
        owner = getattr(self, "sessions", {}).get(key)
        if owner is not None and owner is not session:
            return  # A retired callback must not overwrite or delete its replacement.
        try:
            if session.stopped:
                self.queue_repository.delete(key)
                return
            tracks = []
            current = session.current_song or session.preparing_song
            for song in ([current] if current else []) + session.queue:
                if song.cancelled:
                    continue
                data = {field: getattr(song, field) for field in TRACK_FIELDS}
                data["resume_at"] = self.position(session) if song is session.current_song else song.resume_at
                tracks.append(data)
            self.queue_repository.save(key, {"tracks": tracks, "volume": session.volume,
                "history": session.history[-100:], "sleep_until": session.sleep_until, "sleeping": session.sleeping,
                "loop_mode": session.loop_mode, "text_channel_id": session.last_text_channel_id,
                "panel_channel_id": session.panel_channel_id, "panel_message_id": session.panel_message_id})
        except OSError:
            logger.exception("Queue snapshot failed", extra={"guild_id": session.guild_id, "channel_id": session.channel_id})

    def restore_queues(self):
        if self.queue_repository is None:
            return
        for key, data in self.queue_repository.load().items():
            try:
                guild, channel = map(int, key.split(":"))
                session = ChannelSession(guild_id=guild, channel_id=channel, restored=True)
                volume = data.get("volume", 40)
                if type(volume) is not int or not 0 <= volume <= 100 or data.get("loop_mode", "off") not in {"off", "one", "queue"}:
                    raise ValueError("Invalid player settings")
                session.volume, session.loop_mode = volume, data.get("loop_mode", "off")
                history = data.get("history", [])
                if not isinstance(history, list):
                    raise ValueError("Invalid history")
                session.history = [validate_track(item) for item in history[-100:]]
                session.sleep_until = data.get("sleep_until")
                if session.sleep_until is not None and (type(session.sleep_until) not in (int, float) or not 0 < session.sleep_until < 1e12):
                    raise ValueError("Invalid sleep deadline")
                session.sleeping = bool(data.get("sleeping", False))
                for field, stored in (("last_text_channel_id", "text_channel_id"), ("panel_channel_id", "panel_channel_id"), ("panel_message_id", "panel_message_id")):
                    value = data.get(stored)
                    if value is not None and (type(value) is not int or not 0 < value < 2**64):
                        raise ValueError("Invalid channel/message ID")
                    setattr(session, field, value)
                if not isinstance(data["tracks"], list) or len(data["tracks"]) > 1001:
                    raise ValueError("Invalid queue length")
                for item in data["tracks"]:
                    track = Song(**validate_track(item))
                    offset = float(item.get("resume_at", 0))
                    if not 0 <= offset <= 86400:
                        raise ValueError("Invalid playback offset")
                    track.resume_at = offset
                    session.queue.append(track)
                if session.queue or session.history:
                    self.sessions[key] = session
            except (KeyError, ValueError, TypeError):
                logger.exception("Invalid saved queue %s; file retained", key, extra={"guild_id": key.split(":")[0]})

    async def _continue_saved(self, ctx, session):
        channel = self._get_user_voice_channel(ctx)
        session.stopped = False
        session.sleeping = False
        if session.sleep_until and session.sleep_until <= time.time():
            session.sleep_until = None
        self._remember_session_location(session, ctx, channel)
        session.last_text_channel_id = ctx.channel.id
        if await self._ensure_connected(ctx, channel, session):
            session.restored = False
            await ctx.send(f"Restoring {len(session.queue)} saved tracks.")
            # Do not hold the caller's control lock through media downloads.
            self._spawn(self.play_next(self._channel_key(channel)))

    async def _checkpoint_loop(self):
        while True:
            await asyncio.sleep(15)
            for session in list(self.sessions.values()):
                await self._check_sleep(session)
                if self.sessions.get(self._session_status_key(session)) is not session:
                    continue
                self._persist_session(session)
                if not session.restored and not session.sleeping and not session.current_song and session.queue and session.vc and session.vc.is_connected() and not session.play_lock.locked():
                    self._spawn(self.play_next(self._session_status_key(session)))
                if session.current_song or session.preparing_song or session.panel_message_id:
                    try:
                        await self._refresh_now_playing_panel(session)
                    except Exception:
                        logger.exception("Panel refresh failed; will retry", extra={"guild_id": session.guild_id})
