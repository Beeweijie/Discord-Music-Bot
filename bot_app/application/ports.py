"""Compatibility contracts; feature implementations own their protocols."""
from bot_app.shared.contracts import SettingsRepository, RuntimeRepository, LogReader
from bot_app.features.music.ports import Choice, VoiceConnection
