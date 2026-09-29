import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from bot_app.domain.music.models import Song as Track
from bot_app.domain.music.policies import DEFAULT_VOLUME
from bot_app.application.ports import VoiceConnection

@dataclass
class Song(Track):
    """Event-loop-owned playback/download state around a domain track."""
    resume_at: float = 0
    download_error: Optional[str] = None
    downloaded: bool = False
    downloading: bool = False
    cancelled: bool = False
    suppress_repeat: bool = False
    download_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

@dataclass
class ChannelSession:
    """一个语音频道对应一个播放会话。"""

    queue: List[Song] = field(default_factory=list)
    vc: Optional[VoiceConnection] = None
    last_text_channel_id: Optional[int] = None
    current_song: Optional[Song] = None
    preparing_song: Optional[Song] = None
    panel_channel_id: Optional[int] = None
    panel_message_id: Optional[int] = None
    predownload_task: Optional[asyncio.Task] = None
    idle_task: Optional[asyncio.Task] = None
    volume: int = DEFAULT_VOLUME
    guild_id: Optional[int] = None
    guild_name: Optional[str] = None
    channel_id: Optional[int] = None
    channel_name: Optional[str] = None
    last_command_at: float = field(default_factory=time.monotonic)
    empty_since: Optional[float] = None
    idle_since: Optional[float] = None
    loop_mode: str = "off"
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stopped: bool = False
    generation: int = 0
    playback_id: int = 0
    restored: bool = False
    position_seconds: float = 0
    playing_since: Optional[float] = None
    panel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history: list = field(default_factory=list)
    sleep_until: Optional[float] = None
    sleeping: bool = False
    paused_since: Optional[float] = None
    retiring: bool = False
