from __future__ import annotations
import asyncio
import logging
from bot_app.application.diagnostics import session_logs
import time
from bot_app.domain.music.errors import clean_media_error
from bot_app.domain.music.library import track_data
from pathlib import Path
from bot_app.application.music.models import Song


logger = logging.getLogger(__name__)

class PlaybackOperations:
    async def _download_song_limited(self, song: Song) -> Path:
        """预下载与前台共享同一任务；等待者取消不释放仍在使用的并发槽。"""
        if song.local_path and song.local_path.is_file():
            return song.local_path
        if song.download_error:
            raise RuntimeError(song.download_error)
        if not song.download_task or song.download_task.done():
            song.download_task = self._spawn(self._download_once(song))
        return await asyncio.shield(song.download_task)


    async def _download_once(self, song: Song) -> Path:
        song.downloading = True
        try:
            async with self.download_semaphore:
                if song.cancelled or self.closing:
                    raise RuntimeError("Download cancelled.")
                path = await self.run_media(self._download_song, song)
                if song.cancelled or self.closing:
                    self._spawn(self._delete_file_later(path))
                    raise RuntimeError("Download cancelled.")
                song.local_path = path
                song.downloaded = True
                return path
        except Exception as error:
            song.download_error = clean_media_error(error)
            raise
        finally:
            song.downloading = False


    @session_logs
    async def _predownload_next_two(self, channel_id: str):
        """提前下载队首歌曲，共享下载任务避免出队时重复下载。"""
        session = self.sessions.get(channel_id)
        if not session or session.stopped:
            return
        async with session.queue_lock:
            targets = session.queue[:self.settings_for(session.guild_id)['predownload_count']]
        for song in targets:
            if session.stopped:
                return
            if song.cancelled or not song.is_url:
                continue
            try:
                path = await self._download_song_limited(song)
                self.cache_owners.setdefault(path, set()).add(session.guild_id)
            except Exception as error:
                if not song.cancelled:
                    logger.debug("Preload failed: %s | %s", song.title, error)


    def _start_predownload_task(self, channel_id: int):
        """如果当前没有预下载任务，就启动一个后台预下载任务。"""
        session = self.sessions.get(channel_id)
        if not session or session.stopped:
            return

        if session.predownload_task and not session.predownload_task.done():
            return

        session.predownload_task = self.bot.loop.create_task(
            self._predownload_next_two(channel_id)
        )


    async def _ensure_connected(self, ctx, channel, session):
        """串行连接同一服务器；不抢占另一个频道正在使用的连接。"""
        lock = self.connection_locks.setdefault(ctx.guild.id, asyncio.Lock())
        async with lock:
            if self.closing or session.stopped or self.sessions.get(self._channel_key(channel)) is not session:
                return None
            self._remember_session_location(session, ctx, channel)
            self._cleanup_dead_vc(session)
            me = getattr(ctx.guild, "me", None)
            if me:
                permissions = channel.permissions_for(me)
                if not permissions.connect or not permissions.speak:
                    await ctx.send("❌ I need Connect and Speak permissions in this channel.")
                    return None
            guild_vc = ctx.guild.voice_client
            if guild_vc and guild_vc.is_connected():
                if guild_vc.channel and guild_vc.channel.id != channel.id:
                    old_session = self.sessions.get(self._channel_key(guild_vc.channel))
                    if guild_vc.is_playing() or guild_vc.is_paused() or (old_session and (old_session.queue or old_session.preparing_song)):
                        await ctx.send("Already playing in another voice channel. Use /stop there first.")
                        return None
                    await self._retire_guild_sessions(ctx.guild.id, channel.id)
                    try:
                        await guild_vc.move_to(channel)
                    except Exception as error:
                        await ctx.send(f"❌ Could not move voice channel:{error}")
                        return None
                session.vc = guild_vc
            elif not session.vc:
                try:
                    session.vc = await channel.connect(timeout=20, reconnect=True)
                except Exception as error:
                    await ctx.send(f"❌ Voice connection failed:{error}")
                    return None
            if session.stopped or self.sessions.get(self._channel_key(channel)) is not session:
                if session.vc:
                    await session.vc.disconnect(force=True)
                session.vc = None
                return None
            self._touch_session(session, ctx, channel)
            self._start_idle_task(self._channel_key(channel))
            self._write_music_status(session)
            return session.vc


    async def _play_local_file(self, vc, file_path: Path, channel_id: str, current_song: Song, *, source=None, paused=False):
        session = self.sessions.get(channel_id)
        if not session or session.stopped or current_song.cancelled:
            return
        paused = paused or session.sleeping
        if current_song.is_url:
            self.cache_owners.setdefault(file_path, set()).add(session.guild_id)
        play_generation = session.generation
        offset = current_song.resume_at
        source = source or (self.audio.create_source(file_path, session.volume, position=offset) if offset else self.audio.create_source(file_path, session.volume))
        session.position_seconds = offset
        current_song.resume_at = 0
        session.playing_since = None if paused else time.monotonic()
        session.paused_since = (session.paused_since or time.monotonic()) if paused else None
        session.restored = False
        session.playback_id += 1
        playback_id = session.playback_id
        def after_play(error):
            # discord.py 在音频线程调用这里；所有会话操作回到事件循环。
            def schedule():
                if not self.closing:
                    self._spawn(self._after_playback(channel_id, session, current_song, play_generation, error, playback_id))
            if not self.bot.loop.is_closed():
                self.bot.loop.call_soon_threadsafe(schedule)
        session.current_song = current_song
        session.preparing_song = None
        session.idle_since = None
        try:
            vc.play(source, after=after_play)
            if paused:
                vc.pause()
        except Exception:
            source.cleanup()
            session.current_song = None
            raise
        self._write_music_status(session)

        logger.info("Playing: %s", current_song.title)


    @session_logs
    async def _after_playback(self, channel_id, session, song, generation, error, playback_id=None):
        if playback_id is not None and playback_id != session.playback_id:
            return
        active = self.sessions.get(channel_id) is session and not session.stopped and session.generation == generation
        if active and session.current_song is song:
            async with session.queue_lock:
                session.history.append(track_data(song))
                session.history[:] = session.history[-100:]
                if not error and not song.cancelled and not song.suppress_repeat and session.loop_mode in {"one", "queue"}:
                    if len(session.queue) < self.settings_for(session.guild_id)['max_queue_size']:
                        repeat = self._clone_song(song)
                        if session.loop_mode == "one":
                            session.queue.insert(0, repeat)
                        else:
                            session.queue.append(repeat)
                session.current_song = None
                session.playing_since = None
                session.paused_since = None
                session.position_seconds = 0
            self._write_music_status(session)
            await self._refresh_now_playing_panel(session)
        if song.is_url and song.local_path:
            path = song.local_path
            song.local_path = None
            song.downloaded = False
            self._spawn(self._delete_file_later(path))
        if error:
            logger.warning("音频播放失败: %s", error)
            if active:
                await self._safe_send(self._get_text_channel(session), "❌ Audio playback failed. Trying the next track.")
        if active:
            await self.play_next(channel_id)


    def _set_volume(self, session, volume):
        session.volume = volume
        self.audio.set_volume(session.vc, volume)

    @session_logs
    async def play_next(self, channel_id: int):
        """从队列中取下一首，并按本地文件/远程链接两种路径播放。"""
        session = self.sessions.get(channel_id)
        if not session:
            return

        if session.play_lock.locked():
            return

        async with session.play_lock:
            while True:
                if session.stopped or session.sleeping:
                    return

                self._cleanup_dead_vc(session)
                vc = session.vc
                text_channel = self._get_text_channel(session)

                if not vc or not vc.is_connected():
                    if text_channel:
                        await self._safe_send(text_channel, "Disconnected from voice. Use /resume to continue the queue.")
                    return

                if vc.is_playing() or vc.is_paused():
                    return

                async with session.queue_lock:
                    if not session.queue:
                        session.preparing_song = None
                        self._write_music_status(session)
                        await self._refresh_now_playing_panel(session)
                        return
                    song = session.queue.pop(0)
                if song.cancelled:
                    continue
                session.preparing_song = song
                self._write_music_status(session)

                # 本地 mp3：直接从 assets/music 目录读取。
                if not song.is_url:
                    name = song.input
                    if not name.endswith(".mp3"):
                        name += ".mp3"

                    file_path = self.music_dir / name
                    if not file_path.exists():
                        if text_channel:
                            await self._safe_send(text_channel, f"❌ File not found:`{name}`")
                        continue

                    try:
                        await self._play_local_file(vc, file_path, channel_id, song)
                        if text_channel:
                            await self._send_or_update_now_playing_panel(text_channel, channel_id, session)
                        self._start_predownload_task(channel_id)
                        return
                    except Exception as e:
                        if text_channel:
                            await self._safe_send(text_channel, f"❌ Local playback failed:{clean_media_error(e)}")
                        continue

                # 远程链接：优先使用预下载好的缓存文件，否则现场下载。
                try:
                    if song.local_path and song.local_path.exists():
                        await self._play_local_file(vc, song.local_path, channel_id, song)
                        if text_channel:
                            await self._send_or_update_now_playing_panel(text_channel, channel_id, session)
                        self._start_predownload_task(channel_id)
                        return

                    if song.downloading:
                        waited = 0
                        while song.downloading and waited < 20:
                            if session.stopped or song.cancelled:
                                return
                            await asyncio.sleep(0.5)
                            waited += 0.5

                    if session.stopped or song.cancelled:
                        continue

                    if song.local_path and song.local_path.exists():
                        await self._play_local_file(vc, song.local_path, channel_id, song)
                        if text_channel:
                            await self._send_or_update_now_playing_panel(text_channel, channel_id, session)
                        self._start_predownload_task(channel_id)
                        return

                    song.downloading = True
                    local_path = await self._download_song_limited(song)
                    if session.stopped or song.cancelled:
                        self._spawn(self._delete_file_later(local_path))
                        song.downloading = False
                        continue
                    song.local_path = local_path
                    song.downloaded = True
                    song.downloading = False

                    await self._play_local_file(vc, local_path, channel_id, song)
                    if text_channel:
                        await self._send_or_update_now_playing_panel(text_channel, channel_id, session)

                    self._start_predownload_task(channel_id)
                    return

                except Exception as e:
                    song.downloading = False
                    logger.warning("Track skipped: %s | %s", song.title, clean_media_error(e))
                    if text_channel:
                        await self._safe_send(text_channel, f"❌ Download or playback failed; skipped:{clean_media_error(e)}")
                    if song.local_path:
                        self._spawn(self._delete_file_later(song.local_path))
                        song.local_path = None
                    continue

    async def _seek_session(self, session_id, session, position):
        from bot_app.domain.control import parse_seek_position
        position = parse_seek_position(position)
        vc, song = session.vc, session.current_song
        if session.stopped or session.play_lock.locked() or not song or not vc or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
            raise ValueError("No seekable track. Wait for playback to start.")
        if song.duration and position >= song.duration:
            raise ValueError("Seek position must be before the end of the track.")
        path = song.local_path if song.is_url else self.music_dir / (song.input if song.input.endswith(".mp3") else song.input + ".mp3")
        if not path or not path.is_file():
            raise ValueError("The current audio file is unavailable.")
        source = self.audio.create_source(path, session.volume, position=position)
        paused = vc.is_paused()
        song.resume_at = position
        # Invalidate the stopped player's callback before it can delete this song's cache.
        session.playback_id += 1
        vc.stop()
        await self._play_local_file(vc, path, session_id, song, source=source, paused=paused)
        await self._refresh_now_playing_panel(session)
