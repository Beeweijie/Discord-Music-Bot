"""Discord/FFmpeg source construction stays outside the application layer."""
import discord
from bot_app.features.music.media.cues import ReplyMixer


class DiscordAudio:
    def __init__(self, ffmpeg_path):
        self.ffmpeg_path = ffmpeg_path

    def create_source(self, path, volume, position=0):
        return ReplyMixer(
            discord.FFmpegPCMAudio(str(path), executable=self.ffmpeg_path, before_options=f"-ss {int(position)}", options="-vn"),
            volume=volume / 100,
        )

    def set_volume(self, connection, volume):
        source = getattr(connection, "source", None)
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = volume / 100
