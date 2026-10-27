"""Composition root: the only place that selects concrete service dependencies."""
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def create_speech_recognizer():
    from bot_app.infrastructure.media.speech import SenseVoice
    from bot_app.infrastructure.paths import BASE_DIR
    model = os.getenv("VOICE_MODEL_PATH") or str(BASE_DIR / "data" / "voice-models" /
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17")
    wake = Path(os.getenv("VOICE_WAKE_MODEL_PATH") or str(BASE_DIR / "data" / "voice-models" /
        "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"))
    alternate = BASE_DIR / "data" / "voice-models" / "sherpa-onnx-paraformer-zh-2024-03-09"
    return SenseVoice(model, wake if wake.is_dir() else None, alternate if alternate.is_dir() else None,
        language=os.getenv('VOICE_ASR_LANGUAGE', 'zh'))


def create_voice_replies():
    from bot_app.infrastructure.media.voice_replies import VoiceReplies
    return VoiceReplies()


def create_voice_music_search():
    from bot_app.infrastructure.media.voice_music import VoiceMusicSearch
    return VoiceMusicSearch()


def create_desktop_controller():
    from bot_app.infrastructure.processes.desktop import DesktopProcessController
    return DesktopProcessController()


def create_update_service():
    from bot_app.application.updates import UpdateService
    from bot_app.infrastructure.updates import GitHubUpdater
    from bot_app.infrastructure.paths import BASE_DIR
    return UpdateService(GitHubUpdater(BASE_DIR))


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
