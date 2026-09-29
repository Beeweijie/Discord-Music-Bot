"""Composition root: the only place that selects concrete service dependencies."""
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def create_desktop_controller():
    from bot_app.infrastructure.processes.desktop import DesktopProcessController
    return DesktopProcessController()


def create_welcome_delivery():
    from bot_app.infrastructure.discord.welcome import WelcomeDelivery
    return WelcomeDelivery()


def create_management_service():
    from bot_app.application.management import ManagementService
    from bot_app.infrastructure.paths import BASE_DIR, CONFIG_DIR
    from bot_app.infrastructure.persistence import runtime
    from bot_app.infrastructure.persistence.settings import JsonSettingsRepository
    from bot_app.infrastructure.persistence.logs import FileLogReader
    return ManagementService(runtime, JsonSettingsRepository(CONFIG_DIR), FileLogReader(BASE_DIR / "logs/system.log"))


def create_music_service(bot):
    from bot_app.application.music.service import PlaybackService
    from bot_app.infrastructure.media.source import MediaSource
    from bot_app.infrastructure.media.audio import DiscordAudio
    from bot_app.infrastructure.persistence.music_files import MusicFiles
    from bot_app.infrastructure.persistence import runtime
    from bot_app.presentation.discord.panels import MusicPresenter
    fallback = Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe")
    ffmpeg = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or (
        str(fallback) if os.name == "nt" and fallback.is_file() else "ffmpeg"
    )
    media = MediaSource(ffmpeg)
    service = PlaybackService(bot, media, MusicPresenter(bot), DiscordAudio(ffmpeg),
                           runtime, ThreadPoolExecutor(max_workers=4, thread_name_prefix="media"),
                           MusicFiles(media.music_dir))
    from bot_app.infrastructure.persistence.queues import QueueRepository
    from bot_app.infrastructure.paths import BASE_DIR
    service.queue_repository = QueueRepository(BASE_DIR / "data" / "queues")
    from bot_app.infrastructure.persistence.library import LibraryRepository
    service.library_repository = LibraryRepository(BASE_DIR / "data" / "library")
    service.restore_queues()
    service.settings_repository = bot.management.settings
    return service
