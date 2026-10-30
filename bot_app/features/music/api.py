"""Cross-feature music operations. Playback state never leaves its owner."""
class MusicAPI:
    def __init__(self, service):
        self._service = service

    def configure_connection(self, options):
        self._service.voice_connection_options = dict(options)

    def text_channel(self, guild_id):
        session = self._service.sessions.get(str(guild_id))
        return session.last_text_channel_id if session else None

    async def execute(self, action, context, *args):
        return await self._service.dispatch(action, context, *args)

    async def resolve_voice(self, query, guild_id, user_id, strategy):
        service = self._service
        session = service.sessions.get(str(guild_id))
        titles = [item.title for item in session.queue[:800]] if session else []
        if session:
            titles += [item['title'] for item in session.history[-100:]]
        if service.library_repository:
            try:
                library = service.library_repository.read(guild_id, user_id)
                titles += [item['title'] for item in library['favorites']]
                for playlist in library['playlists'].values():
                    titles += [item['title'] for item in playlist[:100]]
            except ValueError:
                pass
        return await service.run_media(strategy.resolve, query, titles, service.media._search_youtube_results)

    async def speak(self, replies, connection, kind, on_speaking):
        service = self._service
        session = service.sessions.get(str(connection.guild.id))
        if not session or session.vc is not connection or service.closing:
            return False
        try:
            return await replies.say(connection, session, kind, on_speaking=on_speaking)
        finally:
            if (not service.closing and not session.queue_waiting and not session.current_song and session.queue
                    and connection.is_connected() and not connection.is_playing() and not connection.is_paused()):
                service._spawn(service.play_next(str(connection.guild.id)), name='music.after_voice_reply')
