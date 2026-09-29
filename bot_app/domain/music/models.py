from dataclasses import dataclass
from pathlib import Path
from typing import Optional

@dataclass
class Song:
    """队列中的单首歌曲。"""

    input: str
    title: str
    requester_id: int
    requester_name: str
    is_url: bool
    webpage_url: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    uploader: Optional[str] = None
    local_path: Optional[Path] = None
