"""Composition root: the only place that selects concrete service dependencies."""
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

def install_factories(bot):
    from types import SimpleNamespace
    bot.factories = SimpleNamespace(music=lambda: create_music_service(bot),
        welcome_delivery=create_welcome_delivery, speech=create_speech_recognizer,
        voice_replies=create_voice_replies, voice_search=create_voice_music_search,
        ai=lambda: create_ai_services(bot))

def create_ai_services(bot):
    from bot_app.app.ai import AIRuntime
    return AIRuntime(bot.management)


def create_speech_recognizer():
    from bot_app.features.voice_control.recognition import SenseVoice
    from bot_app.shared.paths import BASE_DIR
    model = os.getenv("VOICE_MODEL_PATH") or str(BASE_DIR / "data" / "voice-models" /
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17")
    wake = Path(os.getenv("VOICE_WAKE_MODEL_PATH") or str(BASE_DIR / "data" / "voice-models" /
        "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"))
    alternate = BASE_DIR / "data" / "voice-models" / "sherpa-onnx-paraformer-zh-2024-03-09"
    return SenseVoice(model, wake if wake.is_dir() else None, alternate if alternate.is_dir() else None,
        language=os.getenv('VOICE_ASR_LANGUAGE', 'zh'))


def create_voice_replies():
    from bot_app.features.music.media.cues import VoiceReplies
    return VoiceReplies()


def create_voice_music_search():
    from bot_app.features.music.voice_search import VoiceMusicSearch
    return VoiceMusicSearch()


def create_desktop_controller():
    from bot_app.host.desktop.process_control import DesktopProcessController
    return DesktopProcessController()


def create_update_service():
    from bot_app.features.updates.service import UpdateService
    from bot_app.features.updates.engine import GitHubUpdater
    from bot_app.shared.paths import BASE_DIR
    return UpdateService(GitHubUpdater(BASE_DIR))


def create_welcome_delivery():
    from bot_app.features.welcome.delivery import WelcomeDelivery
    return WelcomeDelivery()


def create_management_service():
    from bot_app.app.management import ManagementService
    from bot_app.shared.paths import BASE_DIR, CONFIG_DIR
    from bot_app.shared import runtime
    from bot_app.app.configuration import JsonSettingsRepository
    from bot_app.shared.logs import FileLogReader
    return ManagementService(runtime, JsonSettingsRepository(CONFIG_DIR), FileLogReader(BASE_DIR / "logs/system.log"))


def create_music_service(bot):
    from bot_app.features.music.service import PlaybackService
    from bot_app.features.music.media.source import MediaSource
    from bot_app.features.music.media.audio import DiscordAudio
    from bot_app.features.music.storage.music_files import MusicFiles
    from bot_app.shared import runtime
    from bot_app.features.music.adapters.panels import MusicPresenter
    fallback = Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe")
    ffmpeg = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or (
        str(fallback) if os.name == "nt" and fallback.is_file() else "ffmpeg"
    )
    media = MediaSource(ffmpeg)
    service = PlaybackService(bot, media, MusicPresenter(bot), DiscordAudio(ffmpeg),
                           runtime, ThreadPoolExecutor(max_workers=4, thread_name_prefix="media"),
                           MusicFiles(media.music_dir))
    from bot_app.features.music.storage.queues import QueueRepository
    from bot_app.shared.paths import BASE_DIR
    service.queue_repository = QueueRepository(BASE_DIR / "data" / "queues")
    from bot_app.features.music.storage.library import LibraryRepository
    service.library_repository = LibraryRepository(BASE_DIR / "data" / "library")
    service.restore_queues()
    service.settings_repository = bot.management.settings
    return service
