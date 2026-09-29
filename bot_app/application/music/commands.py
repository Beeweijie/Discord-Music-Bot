from __future__ import annotations
import logging
import random
from bot_app.domain.music.policies import (
    COMMAND_COOLDOWNS, is_bilibili_url, is_valid_url, is_youtube_url
)


logger = logging.getLogger(__name__)

class CommandsOperations:
    async def _join_impl(self, ctx):
        """加入用户当前所在语音频道。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("Join a voice channel first.")
            return

        session = self._get_session(channel)
        self._remember_session_location(session, ctx, channel)
        self._touch_session(session, ctx, channel)
        session.last_text_channel_id = ctx.channel.id
        session.stopped = False

        vc = await self._ensure_connected(ctx, channel, session)
        if not vc:
            return

        await ctx.send(f"✅ Connected to **{channel.name}**.")


    async def _queue_collection_input(self, ctx, input: str, channel, session):
        """Queue a playlist/collection through the unified play command."""
        try:
            songs = await self.run_media(
                self._extract_collection_songs,
                input,
                ctx.author,
            )

            if session.stopped or self.sessions.get(self._channel_key(channel)) is not session:
                return

            if not songs:
                await ctx.send("❌ No tracks were found.")
                return

            original_count = len(songs)
            songs = self._dedupe_songs(songs)

            async with session.queue_lock:
                existing_keys = {self._song_key(song) for song in session.queue}
                if session.current_song:
                    existing_keys.add(self._song_key(session.current_song))
                songs = [song for song in songs if self._song_key(song) not in existing_keys]

                deduped_count = len(songs)
                if len(songs) > self.settings_for(ctx.guild.id)['max_playlist_songs']:
                    songs = random.sample(songs, self.settings_for(ctx.guild.id)['max_playlist_songs'])
                elif len(songs) > 1:
                    random.shuffle(songs)

                remaining_slots = self.settings_for(ctx.guild.id)['max_queue_size'] - len(session.queue)
                if remaining_slots <= 0:
                    await ctx.send(f"Queue full (limit: {self.settings_for(ctx.guild.id)['max_queue_size']} tracks).")
                    return

                limited_by_queue = len(songs) > remaining_slots
                songs = songs[:remaining_slots]

                if not songs:
                    await ctx.send("All these tracks are already queued.")
                    return

                session.queue.extend(songs)
                queue_size = len(session.queue)

            self._log_session(
                channel.id,
                "playlist queued" if len(songs) > 1 else "song queued",
                guild_id=ctx.guild.id if ctx.guild else 0,
                added=len(songs),
                queue_size=queue_size,
            )
            self._write_music_status(session)

            if len(songs) == 1:
                await ctx.send(self._queued_song_message(songs[0], queue_size))
            else:
                note = f"✅ Added **{len(songs)}** tracks to the queue"
                extras = []
                if original_count != deduped_count:
                    extras.append(f"{original_count - deduped_count} duplicates removed")
                if deduped_count > self.settings_for(ctx.guild.id)['max_playlist_songs']:
                    extras.append(f"randomly selected {self.settings_for(ctx.guild.id)['max_playlist_songs']} tracks")
                if limited_by_queue:
                    extras.append(f"capped at {self.settings_for(ctx.guild.id)['max_queue_size']} queued tracks")
                if extras:
                    note += f"（{'; '.join(extras)}）"
                await ctx.send(note)

            vc = await self._ensure_connected(ctx, channel, session)
            if not vc:
                return

            self._start_predownload_task(self._channel_key(channel))

            if not vc.is_playing() and not vc.is_paused():
                await self.play_next(self._channel_key(channel))

        except Exception as e:
            await ctx.send(f"❌ Could not load playlist:{e}")


    async def _play_impl(self, ctx, input: str | None = None):
        """添加歌曲到队列；未提供输入时使用默认歌单。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("Join a voice channel first.")
            return

        saved = self._get_existing_session(ctx)
        if not (input or "").strip() and saved and saved.restored and saved.queue:
            await self._continue_saved(ctx, saved)
            return
        input = (input or "").strip() or self.settings_for(ctx.guild.id)["default_playlist_url"]
        if not await self._check_cooldown(
            ctx,
            "play",
            COMMAND_COOLDOWNS["play"],
            channel,
            per_user=True,
        ):
            return

        session = self._get_session(channel)
        self._remember_session_location(session, ctx, channel)
        self._touch_session(session, ctx, channel)
        session.last_text_channel_id = ctx.channel.id
        session.stopped = False

        if len(session.queue) >= self.settings_for(ctx.guild.id)['max_queue_size']:
            await ctx.send(f"Queue full (limit: {self.settings_for(ctx.guild.id)['max_queue_size']} tracks).")
            return

        if is_youtube_url(input):
            if self._is_youtube_radio_url(input):
                input = self._normalize_youtube_watch_url(input)
            elif self._is_youtube_playlist_like_url(input):
                await self._queue_collection_input(ctx, input, channel, session)
                return

        if is_bilibili_url(input):
            await self._queue_collection_input(ctx, input, channel, session)
            return

        if not is_valid_url(input) and not self._local_music_exists(input):
            await ctx.send(f"🔎 Searching YouTube for {input[:150]}...")
            try:
                song = await self.run_media(
                    self._search_youtube_song,
                    input,
                    ctx.author,
                )
            except Exception as e:
                await ctx.send(f"❌ Search failed:{e}")
                return
        else:
            song = await self.run_media(
                self._get_song_for_input,
                input,
                ctx.author,
            )

        async with session.queue_lock:
            if session.stopped or self.sessions.get(self._channel_key(channel)) is not session:
                return
            if len(session.queue) >= self.settings_for(ctx.guild.id)['max_queue_size']:
                await ctx.send(f"Queue full (limit: {self.settings_for(ctx.guild.id)['max_queue_size']} tracks).")
                return
            session.queue.append(song)
            queue_size = len(session.queue)

        self._log_session(
            channel.id,
            "song queued",
            guild_id=ctx.guild.id if ctx.guild else 0,
            queue_size=queue_size,
            title=song.title,
        )
        self._write_music_status(session)
        await ctx.send(self._queued_song_message(song, queue_size))

        vc = await self._ensure_connected(ctx, channel, session)
        if not vc:
            return

        self._start_predownload_task(self._channel_key(channel))

        if not vc.is_playing() and not vc.is_paused():
            await self.play_next(self._channel_key(channel))


    async def _queue_impl(self, ctx, page: int = 1):
        session = self._get_existing_session(ctx)
        if not session:
            await ctx.send("Join a voice channel, then use /play.")
            return
        await self.presenter.send_queue(ctx, session, page)


    async def _pause_impl(self, ctx):
        """暂停当前播放。"""
        session = self._get_existing_session(ctx)
        if not session or not session.vc or not session.vc.is_connected():
            await ctx.send("Nothing is playing in this voice channel.")
            return

        if session.vc.is_paused():
            await ctx.send("Playback is already paused.")
            return

        if not session.vc.is_playing():
            await ctx.send("No track is playing.")
            return

        self._pause_session(session)
        await self._refresh_now_playing_panel(session)
        await ctx.send("⏸️ Paused. After 10 minutes, I will disconnect and save your queue.")


    async def _resume_impl(self, ctx):
        session = self._require_session(ctx)
        if session.queue and (session.restored or not session.vc or not session.vc.is_connected()):
            await self._continue_saved(ctx, session)
            return
        session.sleeping = False
        if session.vc and session.vc.is_paused():
            self._resume_session(session)
            await ctx.send("▶️ Playback resumed.")
        elif session.vc and session.vc.is_connected() and session.queue and not session.vc.is_playing():
            self._spawn(self.play_next(self._session_status_key(session)))
            await ctx.send("▶️ Resuming the queue...")
        else:
            await ctx.send("No paused track or saved queue. Use /play to add music.")


    async def _now_impl(self, ctx):
        """显示当前正在播放的歌曲。"""
        session = self._get_existing_session(ctx)
        if not session or not session.current_song:
            await ctx.send("No track is playing.")
            return

        await self._refresh_now_playing_panel(session)
        vc = session.vc
        state = "Paused" if vc and vc.is_paused() else "Playing"
        song = session.current_song
        await ctx.send(
            f"🎧 **{state}：** **{song.title}**（by {song.requester_name}）\n"
            f"Volume: {session.volume}% | Queue: {len(session.queue)} tracks"
        )


    async def _remove_impl(self, ctx, index: int):
        await self._edit_queue(self._require_session(ctx), "remove", index)
        await ctx.send("Track removed from the queue.")


    async def _volume_impl(self, ctx, volume: int):
        session = self._require_session(ctx)
        if not 0 <= volume <= 100:
            raise ValueError("Volume must be between 0 and 100.")
        self._set_volume(session, volume)
        await ctx.send(f"Volume set to {volume}% immediately.")


    async def _shuffle_impl(self, ctx):
        await self._edit_queue(self._require_session(ctx), "shuffle")
        await ctx.send("Queue shuffled. The current track keeps playing.")


    async def _help_impl(self, ctx):
        await ctx.send(
            "**Music: quick start**\n"
            "1. Join a voice channel.\n"
            "2. Use `/play song or URL`, or leave it empty for the default playlist.\n"
            "3. Use the player buttons to add songs, pause, skip, or favorite.\n\n"
            "**Playback**\n"
            "`/pause` · `/resume` · `/skip` · `/replay` · `/volume 0-100`\n"
            "`/seek 1:30` jumps; `/seek +30` forwards; `/seek -15` rewinds.\n"
            "`/loop off|one|queue` · `/sleep minutes` (0 cancels)\n\n"
            "**Queue**\n"
            "`/queue [page]` · `/nextup index` · `/remove index` · `/move from to`\n"
            "`/shuffle` · `/fair` (take turns) · `/dedupe` · `/surprise`\n"
            "`/clear` clears waiting tracks. `/stop` clears everything and disconnects.\n\n"
            "**Your library**\n"
            "`/favorite` · `/favorites [page]` · `/unfavorite index`\n"
            "`/playlist_save name` · `/playlist_load name` · `/playlists`\n"
            "`/playlist_load favorites` plays your favorites.\n"
            "`/playlist_delete name` · `/history [page]`\n\n"
            "All commands also accept `!`. Quote playlist names containing spaces."
        )


    async def _clear_impl(self, ctx):
        """清空当前语音频道的等待队列，不打断正在播放的歌曲。"""
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("Join a voice channel first.")
            return

        session = self.sessions.get(self._channel_key(channel))
        if not session or not session.queue:
            await ctx.send("The queue is already empty.")
            return

        self._touch_session(session, ctx, channel)
        async with session.queue_lock:
            removed = len(session.queue)
            for song in session.queue:
                self._cancel_song(song)
            session.queue.clear()

        self._write_music_status(session)
        await self._refresh_now_playing_panel(session)
        await ctx.send(f"🧹 Cleared {removed} waiting tracks.")


    async def _move_impl(self, ctx, from_index: int, to_index: int):
        await self._edit_queue(self._require_session(ctx), "move", from_index, to_index)
        await ctx.send(f"Moved track {from_index} to position {to_index}.")


    async def _loop_impl(self, ctx, mode: str):
        """设置循环模式。"""
        mode = mode.lower().strip()
        if mode not in {"off", "one", "queue"}:
            await ctx.send("Loop mode must be off, one, or queue.")
            return

        channel = self._get_user_voice_channel(ctx)
        if not channel:
            await ctx.send("Join a voice channel first.")
            return

        session = self._get_session(channel)
        self._touch_session(session, ctx, channel)
        session.loop_mode = mode
        self._write_music_status(session)
        await self._refresh_now_playing_panel(session)

        label = {"off": "Loop disabled.", "one": "Track repeat enabled.", "queue": "Queue repeat enabled."}[mode]
        await ctx.send(f"🔁 {label}")


    async def _replay_impl(self, ctx):
        session = self._require_session(ctx)
        await self._seek_session(self._session_status_key(session), session, 0)
        await ctx.send("Restarted this track. Queue and pause state are unchanged.")


    async def _skip_impl(self, ctx):
        session = self._require_session(ctx)
        channel = self._get_user_voice_channel(ctx)
        if not await self._check_cooldown(ctx, "skip", COMMAND_COOLDOWNS["skip"], channel):
            return
        await self._skip_session(self._session_status_key(session), session)
        await ctx.send("⏭️ Skipped the current track.")


    async def _stop_impl(self, ctx):
        session = self._require_session(ctx)
        await self._dispose_session(self._session_status_key(session))
        await ctx.send("🛑 Stopped, cleared the queue, and disconnected. Favorites and playlists are kept.")


    async def _seek_impl(self, ctx, position: str):
        session = self._get_existing_session(ctx)
        if not session:
            await ctx.send("Join the active voice channel first.")
            return
        try:
            from bot_app.domain.control import parse_seek_position
            text = str(position).strip()
            if text.startswith(("+", "-")):
                delta = parse_seek_position(text[1:]) * (-1 if text[0] == "-" else 1)
                seconds = max(0, int(self.position(session)) + delta)
            else:
                seconds = parse_seek_position(text)
            await self._seek_session(self._channel_key(self._get_user_voice_channel(ctx)), session, seconds)
        except ValueError as error:
            await ctx.send(str(error))
            return
        await ctx.send(f"Seeked to {seconds // 60}:{seconds % 60:02d}")
