from __future__ import annotations
import asyncio
import logging
from bot_app.application.diagnostics import session_logs
import time
import hashlib
import json
from types import SimpleNamespace
from bot_app.domain.music.library import track_data, track_key
from pathlib import Path
from typing import List, Optional
from bot_app.application.music.models import Song, ChannelSession
from bot_app.domain.music.policies import (
    CACHE_CLEANUP_INTERVAL_SECONDS, CACHE_DELETE_DELAY_SECONDS, IDLE_CHECK_INTERVAL_SECONDS
)

logger = logging.getLogger(__name__)

class SessionsOperations:
    def _spawn(self, coroutine):
        task = self.bot.loop.create_task(coroutine)
        self.background_tasks.add(task)
        def finished(done):
            self.background_tasks.discard(done)
            if not done.cancelled() and done.exception():
                logger.warning("音乐后台任务失败", exc_info=done.exception())
        task.add_done_callback(finished)
        return task


    def _get_user_voice_channel(self, ctx) -> Optional[object]:
        """获取命令发送者当前所在的语音频道。"""
        if not getattr(ctx, "guild", None) or not getattr(ctx.author, "voice", None):
            return None
        return ctx.author.voice.channel


    def _session_key(self, guild_id: int, channel_id: int) -> str:
        """使用服务器和频道 ID 隔离会话。"""
        return f"{guild_id}:{channel_id}"


    def _channel_key(self, channel: object) -> str:
        return self._session_key(channel.guild.id, channel.id)


    def _session_status_key(self, session: ChannelSession) -> str:
        if session.guild_id and session.channel_id:
            return self._session_key(session.guild_id, session.channel_id)
        return str(session.channel_id or "unknown")


    def _get_session(self, channel: object) -> ChannelSession:
        """获取或创建语音频道的会话。"""
        key = self._channel_key(channel)
        if key in self.sessions and self.sessions[key].retiring:
            raise ValueError("Disconnecting from this channel. Please retry in a moment.")
        if key not in self.sessions:
            self.sessions[key] = ChannelSession(volume=self.settings_for(channel.guild.id)['default_volume'], guild_id=channel.guild.id, channel_id=channel.id)
        return self.sessions[key]


    def _get_existing_session(self, ctx) -> Optional[ChannelSession]:
        """根据用户当前语音频道取已有会话。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            return None
        return self.sessions.get(self._channel_key(channel))


    def _cleanup_dead_vc(self, session: ChannelSession):
        """清理已经断开的 VoiceClient 引用。"""
        if session.vc and not session.vc.is_connected():
            session.vc = None


    def _get_text_channel(self, session: ChannelSession):
        """取回最近一次发起音乐命令的文字频道。"""
        if session.last_text_channel_id:
            return self.bot.get_channel(session.last_text_channel_id)
        return None


    def _get_context_ids(self, ctx, channel=None):
        """返回 guild/channel/user 标识，用于多服务器隔离 cooldown 和日志。"""
        guild_id = ctx.guild.id if ctx.guild else 0
        channel_id = channel.id if channel else 0
        user_id = ctx.author.id if ctx.author else 0
        return guild_id, channel_id, user_id


    async def _check_cooldown(
        self,
        ctx,
        command_name: str,
        seconds: float,
        channel=None,
        per_user: bool = False,
    ) -> bool:
        """按服务器和语音频道限制高频命令，避免一个服务器影响另一个服务器。"""
        guild_id, channel_id, user_id = self._get_context_ids(ctx, channel)
        cooldown_user = user_id if per_user else 0
        key = (guild_id, channel_id, command_name, cooldown_user)
        now = time.monotonic()
        if len(self.command_cooldowns) > 1000:
            self.command_cooldowns = {
                item_key: item_ready_at
                for item_key, item_ready_at in self.command_cooldowns.items()
                if item_ready_at > now
            }
        ready_at = self.command_cooldowns.get(key, 0)

        if now < ready_at:
            wait_time = max(0.1, ready_at - now)
            await ctx.send(f"Please wait {wait_time:.1f} seconds before trying again.")
            return False

        self.command_cooldowns[key] = now + seconds
        return True


    def _log_session(self, channel_id: int, message: str, **extra):
        """写入带频道/session 信息的调试日志，方便排查多服务器播放问题。"""
        detail = " ".join(f"{key}={value}" for key, value in extra.items())
        if detail:
            logger.info("%s | %s", message, detail, extra={"guild_id": extra.get("guild_id"), "channel_id": channel_id})
        else:
            logger.info("%s", message, extra={"guild_id": extra.get("guild_id"), "channel_id": channel_id})


    def _remember_session_location(self, session: ChannelSession, ctx=None, channel=None):
        """记录 session 所属服务器和语音频道，供托盘 UI 按服务器展示。"""
        if ctx and ctx.guild:
            session.guild_id = ctx.guild.id
            session.guild_name = ctx.guild.name
        if channel:
            session.channel_id = channel.id
            session.channel_name = channel.name
        elif session.vc and session.vc.channel:
            session.channel_id = session.vc.channel.id
            session.channel_name = session.vc.channel.name
            if session.vc.guild:
                session.guild_id = session.vc.guild.id
                session.guild_name = session.vc.guild.name


    def _write_music_status(self, session: Optional[ChannelSession] = None):
        """把当前音乐状态写给托盘小程序读取。"""
        if session:
            owner = self.sessions.get(self._session_status_key(session))
            if owner is not None and owner is not session:
                return
        current = session.current_song if session else None
        if session:
            self._remember_session_location(session)
            human_count = self._human_voice_count(session)
            session_id = self._session_status_key(session)
            self.runtime.write_music_session(
                session_id,
                {
                    "guild_id": session.guild_id,
                    "guild_name": session.guild_name or "-",
                    "channel_id": session.channel_id,
                    "channel_name": session.channel_name or "-",
                    "current_song": current.title if current else None,
                    "requester": current.requester_name if current else None,
                    "queue_size": len(session.queue),
                    "queue_version": self._queue_version(session),
                    "sleep_until": session.sleep_until,
                    "sleeping": session.sleeping,
                    "duration": current.duration if current else None,
                    "thumbnail": current.thumbnail if current else None,
                    "history": session.history[-20:][::-1],
                    "queue": [{"title": song.title, "requester": song.requester_name}
                              for song in session.queue],
                    "restored": session.restored,
                    "position_seconds": int(self.position(session)),
                    "volume": session.volume,
                    "is_connected": bool(session.vc and session.vc.is_connected()),
                    "is_playing": bool(session.vc and session.vc.is_playing()),
                    "is_paused": bool(session.vc and session.vc.is_paused()),
                    "human_count": human_count,
                    "loop_mode": session.loop_mode,
                },
            )
        if session:
            self._persist_session(session)
        self.runtime.write_status(
            current_song=current.title if current else None,
            requester=current.requester_name if current else None,
            queue_size=len(session.queue) if session else 0,
            volume=session.volume if session else self.settings['default_volume'],
        )


    def _touch_session(self, session: ChannelSession, ctx=None, channel=None):
        """记录这个语音频道最近有人使用过音乐指令。"""
        self._remember_session_location(session, ctx, channel)
        session.last_command_at = time.monotonic()


    def _human_voice_count(self, session: ChannelSession) -> int:
        """统计当前语音频道里的真人数量，不把 bot 算进去。"""
        voice_channel = None
        if session.vc and session.vc.channel:
            voice_channel = session.vc.channel
        elif session.channel_id:
            voice_channel = self.bot.get_channel(session.channel_id)

        if not voice_channel or not hasattr(voice_channel, "members"):
            return 0
        return sum(1 for member in voice_channel.members if not member.bot)


    async def on_voice_state_update(self, member, before, after):
        """Start the empty-channel timer as soon as the last human leaves."""
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel and (not after.channel or after.channel.id != before.channel.id):
                old_key = self._channel_key(before.channel)
                session = self.sessions.get(old_key)
                if session and not session.stopped:
                    await self._dispose_session(old_key, disconnect=after.channel is None, preserve=True)
            return
        if member.bot:
            return

        changed_channel_ids = {
            channel.id
            for channel in (before.channel, after.channel)
            if channel is not None
        }
        if not changed_channel_ids:
            return

        now = time.monotonic()
        for session in self.sessions.values():
            if not session.channel_id or session.channel_id not in changed_channel_ids:
                continue
            if self._human_voice_count(session) == 0:
                if session.empty_since is None:
                    session.empty_since = now
            else:
                session.empty_since = None


    def _start_idle_task(self, channel_id: int):
        """为当前语音频道启动自动离开检查。"""
        session = self.sessions.get(channel_id)
        if not session:
            return
        if session.idle_task and not session.idle_task.done():
            return
        session.idle_task = self.bot.loop.create_task(self._idle_watch_loop(channel_id))


    @session_logs
    async def _idle_watch_loop(self, channel_id: int):
        """5 分钟无人或空闲后自动离开语音频道。"""
        while True:
            await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
            session = self.sessions.get(channel_id)
            if not session or session.stopped:
                return

            vc = session.vc
            if not vc or not vc.is_connected():
                return

            now = time.monotonic()
            if vc.is_paused():
                if session.paused_since is None:
                    session.paused_since = now
                if now - session.paused_since >= 600:
                    await self._auto_disconnect_session(channel_id,
                        "Paused for 10 minutes. Disconnected with the queue saved. Use /resume to continue.",
                        preserve=True)
                    return
            else:
                session.paused_since = None
            human_count = self._human_voice_count(session)
            if human_count == 0:
                if session.empty_since is None:
                    session.empty_since = now
                elif now - session.empty_since >= self.settings_for(session.guild_id)['idle_timeout_seconds']:
                    await self._auto_disconnect_session(
                        channel_id,
                        "语音频道持续无人，已退出并保存队列。下次 /resume 可继续。",
                        preserve=True,
                    )
                    return
            else:
                session.empty_since = None

            is_playing = vc.is_playing() or vc.is_paused()
            has_queue = bool(session.queue or session.current_song or session.preparing_song)
            if is_playing or has_queue:
                session.idle_since = None
            elif session.idle_since is None:
                session.idle_since = now
            elif now - session.idle_since >= self.settings_for(session.guild_id)['idle_timeout_seconds']:
                await self._auto_disconnect_session(
                    channel_id,
                    "播放结束后持续空闲，已退出。",
                    preserve=True,
                )
                return

            self._write_music_status(session)


    async def _auto_disconnect_session(self, channel_id: int, reason: str, preserve=False):
        """自动断开一个 session，并清理队列、任务和缓存。"""
        session = self.sessions.get(channel_id)
        if not session:
            return
        text_channel = self._get_text_channel(session)
        await self._dispose_session(channel_id, preserve=preserve)
        if text_channel:
            await self._safe_send(text_channel, f"👋 {reason}")


    @session_logs
    async def _dispose_session(self, channel_id: str, disconnect: bool = True, preserve=False):
        session = self.sessions.get(channel_id)
        if not session or session.retiring:
            return
        session.retiring = True
        parked = None
        if preserve:
            parked = ChannelSession(guild_id=session.guild_id, channel_id=session.channel_id,
                guild_name=session.guild_name, channel_name=session.channel_name,
                last_text_channel_id=session.last_text_channel_id, panel_channel_id=session.panel_channel_id,
                panel_message_id=session.panel_message_id, volume=session.volume, loop_mode=session.loop_mode,
                history=list(session.history), restored=True, sleep_until=session.sleep_until, sleeping=session.sleeping)
            current = session.current_song or session.preparing_song
            parked.queue = [Song(**track_data(s)) for s in ([current] if current else []) + session.queue if not s.cancelled]
            if current and parked.queue:
                parked.queue[0].resume_at = self.position(session) if session.current_song else current.resume_at
            self._persist_session(session)
        session.stopped = True
        if not preserve:
            self._persist_session(session)
        session.generation += 1
        async with session.queue_lock:
            for song in session.queue:
                self._cancel_song(song)
            session.queue.clear()
        if session.preparing_song:
            self._cancel_song(session.preparing_song)
            session.preparing_song = None
        if session.current_song:
            self._cancel_song(session.current_song)
            session.current_song = None
        tasks = [session.predownload_task, session.idle_task]
        for task in tasks:
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        vc = session.vc
        await self._disable_now_playing_panel(session)
        try:
            if vc:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                if disconnect:
                    await vc.disconnect(force=True)
        except Exception:
            logger.exception("断开语音会话失败: %s", channel_id)
        finally:
            session.vc = None
            if self.sessions.get(channel_id) is session:
                self.sessions.pop(channel_id)
            self.runtime.remove_music_session(str(channel_id))
            await asyncio.gather(*(task for task in tasks if task and task is not asyncio.current_task()), return_exceptions=True)
            if parked and (parked.queue or parked.history):
                self.sessions[channel_id] = parked
                self._write_music_status(parked)


    async def _retire_guild_sessions(self, guild_id: int, keep_channel_id: int):
        """同一服务器切换语音频道时，清理旧频道 session，避免旧回调继续调度。"""
        for session_key, session in list(self.sessions.items()):
            if session.channel_id == keep_channel_id:
                continue

            vc_guild_id = session.guild_id
            if vc_guild_id != guild_id:
                continue

            await self._dispose_session(session_key, disconnect=False, preserve=True)
            self._log_session(session.channel_id or 0, "retired stale guild session", guild_id=guild_id)


    def _delete_file_safely(self, file_path: Optional[Path]):
        """删除缓存文件，失败时只打印日志，不影响播放流程。"""
        if not file_path or not self._is_cache_path(file_path) or self._cache_file_in_use(file_path):
            return
        try:
            self.files.delete_cache_file(file_path)
        except Exception as e:
            logger.debug("Cache cleanup deferred: %s", file_path, exc_info=True)


    async def _delete_file_later(self, file_path: Optional[Path]):
        """延迟删除缓存文件，给 FFmpeg 一点释放文件句柄的时间。"""
        if not file_path or not self._is_cache_path(file_path):
            return

        for _ in range(6):
            await asyncio.sleep(CACHE_DELETE_DELAY_SECONDS)
            if self._cache_file_in_use(file_path):
                return
            try:
                if not self.files.exists(file_path):
                    return
                self.files.delete_cache_file(file_path)
                return
            except PermissionError:
                continue
            except Exception as e:
                logger.debug("Cache cleanup deferred: %s", file_path, exc_info=True)
                return

        self._delete_file_safely(file_path)


    def _is_cache_path(self, path: Path) -> bool:
        return self.files.is_cache_path(path)


    def _cache_file_in_use(self, path: Path) -> bool:
        if any(path.name.startswith(prefix + ".") for prefix in self.media.download_prefixes()):
            return True
        for session in list(self.sessions.values()):
            for song in [session.current_song, session.preparing_song, *session.queue]:
                if song and not song.cancelled and song.local_path == path:
                    return True
        return False


    def _cleanup_cache_dir(self):
        """Bot 启动时清空远程音频缓存，避免缓存文件无限增长。"""
        for file_path in self.files.cache_files():
            self._delete_file_safely(file_path)


    async def _cache_cleanup_loop(self):
        """定期清理过期缓存文件，处理异常退出后遗留的下载文件。"""
        while True:
            try:
                await asyncio.sleep(CACHE_CLEANUP_INTERVAL_SECONDS)

                for file_path in self.files.cache_files():
                    try:
                        owners = self.cache_owners.get(file_path, {None})
                        retention = max(self.settings_for(g)["cache_max_age_seconds"] for g in owners)
                        if self.files.modified_at(file_path) <= time.time() - retention:
                            self._delete_file_safely(file_path)
                    except FileNotFoundError:
                        continue
                    except Exception as e:
                        logger.warning("清理缓存文件失败: %s | %s", file_path, e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("缓存清理任务异常: %s", e)


    async def _control_loop(self):
        """逐条领取控制命令，单个错误不阻塞后续操作。"""
        while True:
            try:
                await asyncio.sleep(1)
                for command in self.runtime.drain_control_commands():
                    try:
                        await self.execute_control(command)
                    except Exception as error:
                        logger.warning("Dashboard %s failed: %s", command.get("action"), error, extra={"guild_id": command.get("session_id", "").split(":")[0] or None})
                        self.runtime.write_control_result(command, False, str(error))
                    else:
                        self.runtime.write_control_result(command, True, "Done.")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("读取控制命令失败")


    async def _handle_ui_control(self, command: dict):
        session_id = command.get("session_id")
        action = command.get("action")
        session = self.sessions.get(session_id)
        if not session or session.stopped:
            raise ValueError("This session has ended. Refresh the dashboard.")
        self._touch_session(session)
        vc = session.vc
        if action in {"shuffle", "fair", "dedupe", "surprise", "remove", "nextup", "move"}:
            if "queue_version" in command and command["queue_version"] != self._queue_version(session):
                raise ValueError("The queue changed. Refresh and retry; no tracks were modified.")
            await self._edit_queue(session, action, command.get("index", 0), command.get("to_index", 0))
        elif action == "sleep":
            self._set_sleep(session, command["minutes"])
        elif action == "replay":
            await self._seek_session(session_id, session, 0)
        elif action == "skip":
            await self._skip_session(session_id, session)
        elif action == "stop":
            await self._auto_disconnect_session(session_id, "Playback stopped from the dashboard.")
            return
        elif action == "clear":
            async with session.queue_lock:
                for song in session.queue:
                    self._cancel_song(song)
                session.queue.clear()
        elif action == "seek":
            await self._seek_session(session_id, session, command["position"])
        elif action == "volume":
            volume = int(command.get("volume", self.settings_for(session.guild_id)['default_volume']))
            if not 0 <= volume <= 100:
                raise ValueError("Volume must be between 0 and 100.")
            self._set_volume(session, volume)
        elif action == "loop":
            mode = str(command.get("mode") or "off").lower()
            if mode not in {"off", "one", "queue"}:
                raise ValueError("Invalid loop mode.")
            session.loop_mode = mode
        elif action == "pause":
            if vc and vc.is_playing():
                self._pause_session(session)
            else:
                raise ValueError("No track is playing.")
        elif action == "resume":
            if vc and vc.is_paused():
                self._resume_session(session)
            elif session.queue:
                channel = self.bot.get_channel(session.channel_id)
                if not channel:
                    raise ValueError("The original voice channel no longer exists.")
                if not self._human_voice_count(session):
                    raise ValueError("Join the original voice channel before resuming.")
                class ControlContext:
                    guild = channel.guild
                    author = SimpleNamespace(voice=SimpleNamespace(channel=channel))

                    async def send(self, message):
                        logger.info("网页恢复: %s", message)

                ctx = ControlContext()
                ctx.channel = SimpleNamespace(id=session.last_text_channel_id)
                await self._continue_saved(ctx, session)
                if not session.vc or not session.vc.is_connected():
                    raise ValueError("Could not restore voice. Check the bot log.")
            else:
                raise ValueError("No paused track.")
        else:
            raise ValueError("Unsupported control action.")
        self._write_music_status(session)
        await self._refresh_now_playing_panel(session)


    def _song_key(self, song: Song) -> str:
        """生成歌曲去重 key，优先用输入链接，其次用标题。"""
        return track_key(song)

    def _queue_version(self, session):
        payload = [(s.input, s.requester_id) for s in session.queue]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


    def _dedupe_songs(self, songs: List[Song]) -> List[Song]:
        """按链接/输入去重，保留第一次出现的歌曲。"""
        seen = set()
        unique_songs = []

        for song in songs:
            key = self._song_key(song)
            if not key or key in seen:
                continue
            seen.add(key)
            unique_songs.append(song)

        return unique_songs


    def _cancel_song(self, song: Song):
        """标记歌曲已取消，并安排清理它的缓存文件。"""
        song.cancelled = True
        song.cancel_event.set()
        if song.local_path:
            self._spawn(self._delete_file_later(song.local_path))
            song.local_path = None
            song.downloaded = False


    def _clone_song(self, song: Song) -> Song:
        """复制歌曲；有效缓存可被循环和重播继续使用。"""
        return Song(
            input=song.input,
            title=song.title,
            requester_id=song.requester_id,
            requester_name=song.requester_name,
            is_url=song.is_url,
            webpage_url=song.webpage_url,
            thumbnail=song.thumbnail,
            duration=song.duration,
            uploader=song.uploader,
            local_path=song.local_path,
            downloaded=bool(song.local_path and song.local_path.is_file()),
        )
