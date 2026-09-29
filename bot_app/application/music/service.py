"""Owns music state on the bot event loop; dependencies are injected at startup."""
import asyncio
import logging
from contextvars import copy_context
from bot_app.application.diagnostics import log_scope
from bot_app.domain.control import validate_control
from bot_app.application.music.sessions import SessionsOperations
from bot_app.application.music.playback import PlaybackOperations
from bot_app.application.music.recovery import RecoveryOperations
from bot_app.application.music.commands import CommandsOperations
from bot_app.application.music.extras import ExtrasOperations
from bot_app.domain.music.policies import MAX_DOWNLOAD_CONCURRENCY
from bot_app.domain.settings import MUSIC_DEFAULTS

class PlaybackService(RecoveryOperations, SessionsOperations, PlaybackOperations, CommandsOperations, ExtrasOperations):
    async def run_media(self, operation, *args):
        """Preserve server identity when blocking media work runs in a thread."""
        return await self.bot.loop.run_in_executor(self.executor, copy_context().run, operation, *args)

    def __init__(self, bot, media, presenter, audio, runtime, executor, files):
        self.bot = bot
        self.media = media
        self.presenter = presenter
        self.audio = audio
        self.runtime = runtime
        self.executor = executor
        self.files = files
        self.music_dir = media.music_dir
        self.cache_dir = media.cache_dir
        self.sessions = {}
        self.settings = dict(MUSIC_DEFAULTS)
        self.queue_repository = None
        self.library_repository = None
        self.settings_repository = None
        self.cache_owners = {}
        self.command_cooldowns = {}
        self.download_semaphore = asyncio.Semaphore(MAX_DOWNLOAD_CONCURRENCY)
        self.connection_locks = {}
        self.background_tasks = set()
        self.closing = False
        self.presenter.service = self
        self._cleanup_cache_dir()
        self.cache_cleanup_task = self.bot.loop.create_task(self._cache_cleanup_loop())
        self.control_task = self.bot.loop.create_task(self._control_loop())
        self.checkpoint_task = self.bot.loop.create_task(self._checkpoint_loop())

    def settings_for(self, guild_id):
        if self.settings_repository is not None and guild_id is not None:
            return self.settings_repository.read("music", guild_id)
        return self.settings

    async def close(self):
        if self.closing:
            return
        for session in self.sessions.values():
            self._persist_session(session)
        self.closing = True
        self.checkpoint_task.cancel()
        self.media.close()
        self.cache_cleanup_task.cancel()
        self.control_task.cancel()
        for key in list(self.sessions):
            await self._dispose_session(key)
        await asyncio.gather(self.cache_cleanup_task, self.control_task, self.checkpoint_task, return_exceptions=True)
        # Download tasks observe cancellation through threading.Event; wait for
        # their workers before releasing the executor or deleting their files.
        while self.background_tasks:
            pending = list(self.background_tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)

    async def execute_control(self, command):
        if asyncio.get_running_loop() is not self.bot.loop:
            raise RuntimeError("Music controls must run on the owning event loop")
        if self.closing:
            raise ValueError("The bot is shutting down.")
        command = validate_control(command)
        session = self.sessions.get(command["session_id"])
        if session is None:
            raise ValueError("This session has ended. Refresh the dashboard.")
        async with session.control_lock:
            if self.sessions.get(command["session_id"]) is not session:
                raise ValueError("This session changed. Refresh the dashboard and retry.")
            with log_scope(session.guild_id, session.channel_id):
                result = await self._handle_ui_control(command)
                logging.getLogger(__name__).info("Dashboard: %s", command["action"])
                return result

    async def dispatch(self, action, ctx, *args):
        """Shared member-command boundary for slash, prefix and button inputs.

        Short mutations are serialized with administrator controls. Resolving
        media never holds this lock; queue_lock and generation checks protect
        the later commit, so Stop remains responsive during a download.
        """
        allowed = {"join", "play", "queue", "pause", "resume", "now", "remove", "volume",
                   "seek", "shuffle", "skip", "stop", "help", "clear", "move", "loop", "replay",
                   "fair", "dedupe", "nextup", "surprise", "sleep", "history", "favorite", "favorites",
                   "unfavorite", "playlist_save", "playlist_load", "playlists", "playlist_delete"}
        if action not in allowed:
            raise ValueError("Unsupported action.")
        if self.closing:
            await ctx.send("The bot is shutting down. Please retry later.")
            return
        if not getattr(ctx, "guild", None):
            await ctx.send("Use music commands in a server.")
            return
        try:
            voice = self._get_user_voice_channel(ctx)
            with log_scope(ctx.guild.id, getattr(voice, "id", None)):
                level = logging.DEBUG if action in {"queue", "now", "help", "history", "favorites", "playlists"} else logging.INFO
                logging.getLogger(__name__).log(level, "Requested: %s", action)
                return await self._dispatch_checked(action, ctx, *args)
        except ValueError as error:
            await ctx.send(str(error))

    async def _dispatch_checked(self, action, ctx, *args):
        handler = getattr(self, f"_{action}_impl")
        session = self._get_existing_session(ctx)
        if session and action not in {"join", "play", "playlist_load", "queue", "now", "help"}:
            async with session.control_lock:
                if self.sessions.get(self._session_status_key(session)) is not session or session.stopped:
                    raise ValueError("This session has ended. Use /resume or /play to continue.")
                result = await handler(ctx, *args)
                if self.sessions.get(self._session_status_key(session)) is session:
                    self._write_music_status(session)
                    await self._refresh_now_playing_panel(session)
                return result
        result = await handler(ctx, *args)
        session = self._get_existing_session(ctx)
        if session:
            self._write_music_status(session)
        return result

    async def _skip_session(self, session_id, session):
        vc = session.vc
        if not vc or not vc.is_connected():
            raise ValueError("Not connected to voice.")
        if session.play_lock.locked():
            raise ValueError("Preparing a track. Please wait a moment.")
        if vc.is_playing() or vc.is_paused():
            if session.current_song:
                session.current_song.suppress_repeat = True
            vc.stop()
        else:
            self._spawn(self.play_next(session_id))

    def _normalize_youtube_playlist_url(self, *args, **kwargs):
        return self.media._normalize_youtube_playlist_url(*args, **kwargs)

    def _normalize_youtube_watch_url(self, *args, **kwargs):
        return self.media._normalize_youtube_watch_url(*args, **kwargs)

    def _compact_youtube_video_url(self, *args, **kwargs):
        return self.media._compact_youtube_video_url(*args, **kwargs)

    def _youtube_search_entry_url(self, *args, **kwargs):
        return self.media._youtube_search_entry_url(*args, **kwargs)

    def _is_youtube_radio_url(self, *args, **kwargs):
        return self.media._is_youtube_radio_url(*args, **kwargs)

    def _is_youtube_playlist_like_url(self, *args, **kwargs):
        return self.media._is_youtube_playlist_like_url(*args, **kwargs)

    def _build_ydl_opts(self, *args, **kwargs):
        return self.media._build_ydl_opts(*args, **kwargs)

    def _apply_site_opts(self, *args, **kwargs):
        return self.media._apply_site_opts(*args, **kwargs)

    def _clean_song_title(self, *args, **kwargs):
        return self.media._clean_song_title(*args, **kwargs)

    def _is_position_like_title(self, *args, **kwargs):
        return self.media._is_position_like_title(*args, **kwargs)

    def _title_from_info(self, *args, **kwargs):
        return self.media._title_from_info(*args, **kwargs)

    def _create_song(self, *args, **kwargs):
        return self.media._create_song(*args, **kwargs)

    def _create_song_from_info(self, *args, **kwargs):
        return self.media._create_song_from_info(*args, **kwargs)

    def _get_title_for_input(self, *args, **kwargs):
        return self.media._get_title_for_input(*args, **kwargs)

    def _get_song_for_input(self, *args, **kwargs):
        return self.media._get_song_for_input(*args, **kwargs)

    def _local_music_exists(self, *args, **kwargs):
        return self.media._local_music_exists(*args, **kwargs)

    def _resolve_local_music(self, *args, **kwargs):
        return self.media._resolve_local_music(*args, **kwargs)

    def _local_music_choices(self, *args, **kwargs):
        return self.media._local_music_choices(*args, **kwargs)

    def _search_youtube_song(self, *args, **kwargs):
        return self.media._search_youtube_song(*args, **kwargs)

    def _search_youtube_results(self, *args, **kwargs):
        return self.media._search_youtube_results(*args, **kwargs)

    def _search_youtube_choices(self, *args, **kwargs):
        return self.media._search_youtube_choices(*args, **kwargs)

    def _extract_collection_songs(self, *args, **kwargs):
        return self.media._extract_collection_songs(*args, **kwargs)

    def _parse_collection_info_to_songs(self, *args, **kwargs):
        return self.media._parse_collection_info_to_songs(*args, **kwargs)

    def _download_song(self, *args, **kwargs):
        return self.media._download_song(*args, **kwargs)

    def _locate_downloaded_file(self, *args, **kwargs):
        return self.media._locate_downloaded_file(*args, **kwargs)

    def _queued_song_message(self, *args, **kwargs):
        return self.presenter._queued_song_message(*args, **kwargs)

    def _format_duration(self, *args, **kwargs):
        return self.presenter._format_duration(*args, **kwargs)

    def _build_now_playing_embed(self, *args, **kwargs):
        return self.presenter._build_now_playing_embed(*args, **kwargs)

    async def _refresh_now_playing_panel(self, *args, **kwargs):
        return await self.presenter._refresh_now_playing_panel(*args, **kwargs)

    async def _send_or_update_now_playing_panel(self, *args, **kwargs):
        return await self.presenter._send_or_update_now_playing_panel(*args, **kwargs)

    async def _disable_now_playing_panel(self, *args, **kwargs):
        return await self.presenter._disable_now_playing_panel(*args, **kwargs)

    async def _safe_send(self, *args, **kwargs):
        return await self.presenter._safe_send(*args, **kwargs)
