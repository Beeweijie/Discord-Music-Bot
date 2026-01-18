from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent


# 📁 config 配置路径
CONFIG_DIR = BASE_DIR / "config"
EMOJI_JSON = CONFIG_DIR / "emoji.json"
MUSIC_JSON = CONFIG_DIR / "music.json"

# 📁 assets 静态资源路径
ASSETS_DIR = BASE_DIR / "assets"
MUSIC_DIR = ASSETS_DIR / "music"