"""项目路径集中配置。

其他模块统一从这里拿路径，避免在各处手写相对路径。
"""

from pathlib import Path


# 项目根目录：Discord-Music-Bot/
BASE_DIR = Path(__file__).resolve().parent.parent


# config 配置目录
CONFIG_DIR = BASE_DIR / "config"
EMOJI_JSON = CONFIG_DIR / "emoji.json"
MUSIC_JSON = CONFIG_DIR / "music.json"


# assets 静态资源目录
ASSETS_DIR = BASE_DIR / "assets"
MUSIC_DIR = ASSETS_DIR / "music"
