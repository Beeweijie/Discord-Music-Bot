from urllib.parse import urlparse

DEFAULT_VOLUME = 40
MAX_QUEUE_SIZE = 500
MAX_PLAYLIST_SONGS = 500
PREDOWNLOAD_COUNT = 3
MAX_DOWNLOAD_CONCURRENCY = 2
CACHE_DELETE_DELAY_SECONDS = 5
CACHE_CLEANUP_INTERVAL_SECONDS = 15 * 60
CACHE_FILE_MAX_AGE_SECONDS = 60 * 60
VOICE_IDLE_TIMEOUT_SECONDS = 5 * 60
IDLE_CHECK_INTERVAL_SECONDS = 30
COMMAND_COOLDOWNS = {
    "play": 1.2,
    "remove": 1.0,
    "shuffle": 2.0,
    "skip": 1.5,
    "stop": 3.0,
}

def is_valid_url(url: str) -> bool:
    """判断输入是否是 http/https 链接。"""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _has_host(url: str, domains) -> bool:
    if not is_valid_url(url):
        return False
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def is_youtube_url(url: str) -> bool:
    """判断输入是否是 YouTube 链接。"""
    return _has_host(url, ("youtube.com", "youtu.be"))


def is_bilibili_url(url: str) -> bool:
    """判断输入是否是 Bilibili 链接。"""
    return _has_host(url, ("bilibili.com", "b23.tv"))
