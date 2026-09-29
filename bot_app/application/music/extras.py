"""Optional music conveniences. All entry points still use PlaybackService.dispatch."""
import random
import time
from bot_app.application.music.models import Song
from bot_app.domain.music.library import track_data, track_key, fair_queue


class ExtrasOperations:
    def _require_session(self, ctx):
        session = self._get_existing_session(ctx)
        if not session:
            raise ValueError("Join a voice channel and use /play first.")
        return session

    async def _edit_queue(self, session, action, index=0, to_index=0):
        async with session.queue_lock:
            if not session.queue:
                raise ValueError("The waiting queue is empty.")
            if action in {"remove", "move", "nextup"} and not 1 <= index <= len(session.queue):
                raise ValueError("Track positions changed. Refresh the queue and retry.")
            if action == "remove":
                self._cancel_song(session.queue.pop(index - 1))
            elif action in {"move", "nextup"}:
                target = 1 if action == "nextup" else to_index
                if not 1 <= target <= len(session.queue):
                    raise ValueError("Target position is outside the queue.")
                session.queue.insert(target - 1, session.queue.pop(index - 1))
            elif action == "shuffle":
                random.shuffle(session.queue)
            elif action == "fair":
                session.queue[:] = fair_queue(session.queue)
            elif action == "dedupe":
                seen, kept = set(), []
                for song in session.queue:
                    key = track_key(song)
                    if key in seen:
                        self._cancel_song(song)
                    else:
                        seen.add(key)
                        kept.append(song)
                session.queue[:] = kept
            elif action == "surprise":
                session.queue.insert(0, session.queue.pop(random.randrange(len(session.queue))))
        self._write_music_status(session)
        self._start_predownload_task(self._session_status_key(session))
        await self._refresh_now_playing_panel(session)

    async def _fair_impl(self, ctx):
        await self._edit_queue(self._require_session(ctx), "fair")
        await ctx.send("Queue reordered by requester. Each person's track order is preserved.")

    async def _dedupe_impl(self, ctx):
        session = self._require_session(ctx)
        before = len(session.queue)
        await self._edit_queue(session, "dedupe")
        await ctx.send(f"Removed {before - len(session.queue)} duplicate tracks.")

    async def _nextup_impl(self, ctx, index: int):
        await self._edit_queue(self._require_session(ctx), "nextup", index)
        await ctx.send("Moved to the front. It will play after the current track.")

    async def _surprise_impl(self, ctx):
        session = self._require_session(ctx)
        await self._edit_queue(session, "surprise")
        await ctx.send(f"🎲 Surprise next: {session.queue[0].title[:180]}. The current track keeps playing.")

    async def _sleep_impl(self, ctx, minutes: int = 0):
        session = self._require_session(ctx)
        self._set_sleep(session, minutes)
        await ctx.send(f"Playback will pause in {minutes} minutes. Your queue is kept; /resume continues it." if minutes else "Sleep timer cancelled.")

    def _set_sleep(self, session, minutes):
        if type(minutes) is not int or not 0 <= minutes <= 1440:
            raise ValueError("Enter 0-1440 minutes; 0 cancels the timer.")
        session.sleep_until = time.time() + minutes * 60 if minutes else None
        self._write_music_status(session)

    async def _check_sleep(self, session):
        if session.sleep_until is None or time.time() < session.sleep_until:
            return
        async with session.control_lock:
            if session.stopped or session.sleep_until is None or time.time() < session.sleep_until:
                return
            session.sleep_until = None
            session.sleeping = True
            if session.vc and session.vc.is_playing():
                self._pause_session(session)
            self._write_music_status(session)
        await self._refresh_now_playing_panel(session)
        await self._safe_send(self._get_text_channel(session), "💤 Sleep timer: paused with your queue saved. Use /resume to continue.")

    async def _history_impl(self, ctx, page: int = 1):
        session = self._require_session(ctx)
        tracks = list(reversed(session.history))
        await self._send_tracks(ctx, "Recently played (including skipped tracks)", tracks, page)

    async def _send_tracks(self, ctx, title, tracks, page=1):
        pages = max(1, (len(tracks) + 9) // 10)
        if not 1 <= page <= pages:
            raise ValueError(f"Page must be between 1 and {pages}.")
        start = (page - 1) * 10
        lines = [f"{i + 1}. {s['title'][:100]}" for i, s in enumerate(tracks[start:start + 10], start)]
        await ctx.send(f"**{title}** · Page {page}/{pages} · {len(tracks)} tracks\n" + ("\n".join(lines) or "No tracks yet."))

    async def _favorite_impl(self, ctx):
        session = self._require_session(ctx)
        if not session.current_song:
            raise ValueError("No current track to favorite.")
        data = self.library_repository.read(ctx.guild.id, ctx.author.id)
        track = session.current_song
        if any(track_key(Song(**s)) == track_key(track) for s in data["favorites"]):
            await ctx.send("This track is already in your favorites.")
            return
        if len(data["favorites"]) >= 500:
            raise ValueError("Favorites are full (500 tracks). Use /unfavorite to make room.")
        data["favorites"].append(track_data(track))
        self.library_repository.save(ctx.guild.id, ctx.author.id, data)
        await ctx.send(f"❤️ Saved: {track.title[:180]}. View /favorites or play with /playlist_load favorites.")

    async def _favorites_impl(self, ctx, page: int = 1):
        data = self.library_repository.read(ctx.guild.id, ctx.author.id)
        await self._send_tracks(ctx, "My favorites", data["favorites"], page)

    async def _unfavorite_impl(self, ctx, index: int):
        data = self.library_repository.read(ctx.guild.id, ctx.author.id)
        if not 1 <= index <= len(data["favorites"]):
            raise ValueError("Invalid position. Use /favorites to check your list.")
        removed = data["favorites"].pop(index - 1)
        self.library_repository.save(ctx.guild.id, ctx.author.id, data)
        await ctx.send(f"Removed favorite: {removed['title'][:180]}")

    async def _playlist_save_impl(self, ctx, name: str):
        name = name.strip()
        if not name or len(name) > 40 or name.casefold() == "favorites":
            raise ValueError("Playlist names must be 1-40 characters. The name favorites is reserved.")
        session = self._require_session(ctx)
        tracks = ([session.current_song or session.preparing_song] if session.current_song or session.preparing_song else []) + session.queue
        if not tracks:
            raise ValueError("No tracks to save.")
        data = self.library_repository.read(ctx.guild.id, ctx.author.id)
        if name in data["playlists"]:
            raise ValueError("That playlist exists. Choose another name or remove it with /playlist_delete first.")
        if len(data["playlists"]) >= 20:
            raise ValueError("You can save up to 20 personal playlists.")
        data["playlists"][name] = [track_data(s) for s in tracks]
        self.library_repository.save(ctx.guild.id, ctx.author.id, data)
        await ctx.send(f"Saved playlist [{name}] with {len(tracks)} tracks. Tracks start from the beginning.")

    async def _playlists_impl(self, ctx):
        data = self.library_repository.read(ctx.guild.id, ctx.author.id)
        lines = [f"favorites: {len(data['favorites'])} tracks"]
        lines += [f"{name}: {len(tracks)} tracks" for name, tracks in data["playlists"].items()]
        await ctx.send("**My playlists (this server)**\n" + "\n".join(lines) + "\nUse /playlist_load name to add tracks to the queue.")

    async def _playlist_delete_impl(self, ctx, name: str):
        data = self.library_repository.read(ctx.guild.id, ctx.author.id)
        if name not in data["playlists"]:
            raise ValueError("Playlist not found. Use /playlists to see your library.")
        del data["playlists"][name]
        self.library_repository.save(ctx.guild.id, ctx.author.id, data)
        await ctx.send(f"Deleted playlist [{name[:40]}]. Playback is unchanged.")

    async def _playlist_load_impl(self, ctx, name: str):
        channel = self._get_user_voice_channel(ctx)
        if not channel:
            raise ValueError("Join a voice channel first.")
        data = self.library_repository.read(ctx.guild.id, ctx.author.id)
        tracks = data["favorites"] if name.casefold() == "favorites" else data["playlists"].get(name)
        if not tracks:
            raise ValueError("Playlist is empty or missing. Use /playlists to check.")
        session = self._get_session(channel)
        self._remember_session_location(session, ctx, channel)
        session.last_text_channel_id = ctx.channel.id
        songs = [Song(**dict(item, requester_id=ctx.author.id, requester_name=str(ctx.author))) for item in tracks]
        async with session.queue_lock:
            remaining = max(0, self.settings_for(ctx.guild.id)["max_queue_size"] - len(session.queue))
            if not remaining:
                raise ValueError("The waiting queue is full.")
            session.queue.extend(songs[:remaining])
        self._write_music_status(session)
        await ctx.send(f"Added {min(remaining, len(songs))}/{len(songs)} tracks in playlist order.")
        vc = await self._ensure_connected(ctx, channel, session)
        if vc and not vc.is_playing() and not vc.is_paused():
            session.restored = False
            session.sleeping = False
            self._spawn(self.play_next(self._channel_key(channel)))
        self._start_predownload_task(self._channel_key(channel))
