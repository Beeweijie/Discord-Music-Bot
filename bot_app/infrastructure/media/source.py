"""Blocking media operations. Workers return results; they never mutate sessions."""
import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from bot_app.domain.music.errors import clean_media_error
import yt_dlp
from bot_app.application.music.models import Song
from bot_app.application.ports import Choice
from bot_app.domain.music.policies import is_valid_url, is_youtube_url, is_bilibili_url
from bot_app.infrastructure.paths import CONFIG_DIR, MUSIC_DIR

logger = logging.getLogger(__name__)


class DownloaderLog:
    """Keep extractor chatter in diagnostics; playback reports the final failure once."""
    def debug(self, message):
        logger.debug("Extractor: %s", message)

    def warning(self, message):
        logger.debug("Extractor warning: %s", message)

    def error(self, message):
        logger.debug("Extractor error: %s", message)

NODE_JS_PATH = shutil.which("node")
BILIBILI_COOKIE_FILE = CONFIG_DIR / "bilibili_cookies.txt"
BILIBILI_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class MediaSource:
    def __init__(self, ffmpeg_path, music_dir=MUSIC_DIR):
        self.ffmpeg_path = ffmpeg_path
        self.music_dir = Path(music_dir)
        self.cache_dir = self.music_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.search_cache = {}
        self.cache_lock = threading.Lock()
        self.active_downloads = set()
        self.download_lock = threading.Lock()
        self.close_event = threading.Event()

    def _add_download(self, key):
        with self.download_lock:
            self.active_downloads.add(key)

    def _remove_download(self, key):
        with self.download_lock:
            self.active_downloads.discard(key)

    def download_prefixes(self):
        with self.download_lock:
            return self.active_downloads.copy()

    def close(self):
        self.close_event.set()

    def _normalize_youtube_playlist_url(self, url: str) -> str:
        """把 watch?v=xxx&list=yyy 形式统一成标准 playlist URL。"""
        match = re.search(r"list=([A-Za-z0-9_\-]+)", url)
        if match:
            list_id = match.group(1)
            return f"https://www.youtube.com/playlist?list={list_id}"
        return url


    def _normalize_youtube_watch_url(self, url: str) -> str:
        """把 YouTube radio/mix 链接转成普通 watch?v=... 链接。"""
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        video_ids = query.get("v")
        if not video_ids:
            return url

        new_query = urlencode({"v": video_ids[0]})
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_query, ""))


    def _compact_youtube_video_url(self, url: str, video_id: Optional[str] = None) -> Optional[str]:
        """把 YouTube 搜索结果压成不会超过 autocomplete 限制的 watch URL。"""
        candidate = str(url or "").strip()
        parsed = urlparse(candidate)
        host = parsed.netloc.lower()

        if not video_id:
            if "youtu.be" in host:
                video_id = parsed.path.strip("/").split("/")[0]
            elif "youtube.com" in host:
                query = parse_qs(parsed.query)
                video_id = (query.get("v") or [""])[0]
                if not video_id:
                    parts = [part for part in parsed.path.split("/") if part]
                    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
                        video_id = parts[1]
            elif candidate and not candidate.startswith("http"):
                video_id = candidate

        if not video_id:
            return None
        return f"https://www.youtube.com/watch?v={video_id}"


    def _youtube_search_entry_url(self, entry: dict) -> Optional[str]:
        """从 yt-dlp 搜索结果里拿可播放的 YouTube 链接。"""
        entry_id = entry.get("id")
        entry_url = (
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
            or entry_id
        )
        raw_url = str(entry_url or "")
        if raw_url.startswith("http") and not is_youtube_url(raw_url):
            return None
        compact_url = self._compact_youtube_video_url(raw_url, str(entry_id or ""))
        if compact_url:
            return compact_url
        if raw_url and is_youtube_url(raw_url):
            return raw_url
        return None


    def _is_youtube_radio_url(self, url: str) -> bool:
        """判断是否是 YouTube 自动生成的 radio/mix 链接。"""
        if not is_youtube_url(url):
            return False

        query = parse_qs(urlparse(url).query)
        list_id = (query.get("list") or [""])[0]
        return bool(query.get("v")) and (
            query.get("start_radio") == ["1"]
            or list_id.startswith("RD")
        )


    def _is_youtube_playlist_like_url(self, url: str) -> bool:
        """判断 /play 输入是否是普通 YouTube 播放列表。"""
        if not is_youtube_url(url) or "list=" not in url:
            return False
        return not self._is_youtube_radio_url(url)


    def _build_ydl_opts(
        self,
        output_template: Optional[str] = None,
        allow_playlist: bool = False,
        extract_flat: bool = False,
    ) -> dict:
        """构造 yt-dlp 参数；下载时会额外配置音频转码。"""
        opts = {
            "quiet": True, "logger": DownloaderLog(),
            "no_color": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "ffmpeg_location": self.ffmpeg_path,
        }

        if NODE_JS_PATH:
            opts["js_runtimes"] = {"node": {"path": NODE_JS_PATH}}

        if not allow_playlist:
            opts["noplaylist"] = True

        if extract_flat:
            opts["extract_flat"] = "in_playlist"

        if output_template is not None:
            opts.update({
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "restrictfilenames": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "128",
                    }
                ],
            })

        return opts


    def _apply_site_opts(self, url: str, ydl_opts: dict) -> dict:
        """Add site-specific yt-dlp options without affecting other platforms."""
        if is_bilibili_url(url):
            headers = dict(ydl_opts.get("http_headers") or {})
            headers.update(BILIBILI_HTTP_HEADERS)
            ydl_opts["http_headers"] = headers
            if BILIBILI_COOKIE_FILE.exists():
                ydl_opts["cookiefile"] = str(BILIBILI_COOKIE_FILE)
        return ydl_opts


    def _clean_song_title(
        self,
        title: Optional[str],
        source_url: str = "",
        fallback: str = "未知标题",
    ) -> str:
        """Normalize noisy extractor titles before showing them in Discord."""
        cleaned = (title or fallback or "").strip()
        if not cleaned:
            cleaned = fallback

        if is_bilibili_url(source_url):
            cleaned = re.sub(r"\s*[_-]\s*哔哩哔哩(?:_bilibili)?\s*$", "", cleaned)
            cleaned = re.sub(r"\s*\|\s*哔哩哔哩(?:\s*bilibili)?\s*$", "", cleaned, flags=re.I)
            cleaned = re.sub(r"\s*-\s*bilibili\s*$", "", cleaned, flags=re.I)

        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or fallback


    def _is_position_like_title(self, title: str) -> bool:
        """Bilibili multipart entries can expose only a page/position label."""
        value = (title or "").strip().lower()
        if not value:
            return True
        return bool(
            re.fullmatch(r"(?:p|part|ep)?\s*\d{1,4}", value)
            or re.fullmatch(r"第\s*\d{1,4}\s*(?:p|集|话|段|部分)", value)
        )


    def _title_from_info(self, info: dict, fallback: str, source_url: str = "") -> str:
        if is_bilibili_url(source_url):
            title = (
                info.get("fulltitle")
                or info.get("title")
                or info.get("alt_title")
                or info.get("part")
                or fallback
            )
        else:
            title = (
                info.get("title")
                or info.get("fulltitle")
                or info.get("alt_title")
                or info.get("part")
                or fallback
            )
        return self._clean_song_title(title, source_url, fallback)


    def _create_song(self, input_str: str, title: str, requester) -> Song:
        """把解析结果包装成队列 Song 对象。"""
        return Song(
            input=input_str,
            title=self._clean_song_title(title, input_str),
            requester_id=requester.id,
            requester_name=requester.display_name,
            is_url=is_valid_url(input_str),
            webpage_url=input_str if is_valid_url(input_str) else None,
        )


    def _create_song_from_info(
        self,
        input_str: str,
        info: dict,
        requester,
        fallback_title: str = "未知标题",
    ) -> Song:
        """Create a Song with display metadata when yt-dlp exposes it."""
        webpage_url = (
            info.get("webpage_url")
            or info.get("original_url")
            or (input_str if is_valid_url(input_str) else None)
        )
        title = self._title_from_info(info, fallback_title, str(webpage_url or input_str))
        return Song(
            input=input_str,
            title=title,
            requester_id=requester.id,
            requester_name=requester.display_name,
            is_url=is_valid_url(input_str),
            webpage_url=str(webpage_url) if webpage_url else None,
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
            uploader=info.get("uploader") or info.get("channel") or info.get("artist"),
        )


    def _get_title_for_input(self, input_str: str) -> str:
        """获取本地文件名或远程链接标题。"""
        if not is_valid_url(input_str):
            name = input_str
            if name.endswith(".mp3"):
                name = name[:-4]
            return name

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=None,
                allow_playlist=False,
                extract_flat=False,
            )
            ydl_opts = self._apply_site_opts(input_str, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
                return self._title_from_info(info, input_str, input_str)
        except Exception:
            pass

        try:
            ydl_opts = {
                "quiet": True, "logger": DownloaderLog(),
                "no_warnings": True,
                "noprogress": True,
                "noplaylist": True,
            }
            ydl_opts = self._apply_site_opts(input_str, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
                return self._title_from_info(info, input_str, input_str)
        except Exception:
            return input_str


    def _get_song_for_input(self, input_str: str, requester) -> Song:
        """Resolve one local file or remote URL into a Song."""
        if not is_valid_url(input_str):
            title = input_str[:-4] if input_str.endswith(".mp3") else input_str
            return self._create_song(input_str, title, requester)

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=None,
                allow_playlist=False,
                extract_flat=False,
            )
            ydl_opts = self._apply_site_opts(input_str, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(input_str, download=False)
            return self._create_song_from_info(input_str, info, requester, input_str)
        except Exception:
            title = self._get_title_for_input(input_str)
            return self._create_song(input_str, title, requester)


    def _local_music_exists(self, input_str: str) -> bool:
        """检查输入是否对应 assets/music 下的本地 mp3。"""
        return self._resolve_local_music(input_str) is not None


    def _resolve_local_music(self, input_str: str) -> Optional[Path]:
        name = input_str.strip()
        if not name or "/" in name or "\\" in name or ":" in name:
            return None
        if not name.lower().endswith(".mp3"):
            name += ".mp3"
        root = self.music_dir.resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            return None
        return path


    def _local_music_choices(self, query: str) -> List[Choice]:
        """根据输入返回本地 mp3 自动补全候选。"""
        query_lower = query.lower()
        choices = []

        for file_path in sorted(self.music_dir.glob("*.mp3")):
            name = file_path.stem
            if len(name) > 100 or not self._resolve_local_music(name):
                continue
            if query_lower and query_lower not in name.lower():
                continue
            choices.append(Choice(name=f"本地：{name}"[:100], value=name))
            if len(choices) >= 5:
                break

        return choices


    def _search_youtube_song(self, query: str, requester) -> Song:
        """用 yt-dlp 搜索 YouTube 第一条结果，并转换成 Song。"""
        search_query = f"ytsearch1:{query}"

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=None,
                allow_playlist=False,
                extract_flat=True,
            )
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
        except Exception:
            try:
                ydl_opts = {
                    "quiet": True, "logger": DownloaderLog(),
                    "no_warnings": True,
                    "noprogress": True,
                    "extract_flat": True,
                    "noplaylist": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_query, download=False)
            except Exception as e:
                raise RuntimeError(f"YouTube search failed:{e}")

        entries = info.get("entries") or []
        if not entries:
            raise RuntimeError("No matching YouTube results.")

        entry = entries[0]
        entry_url = self._youtube_search_entry_url(entry)

        if not entry_url:
            raise RuntimeError("Search returned no playable URL.")

        return self._create_song_from_info(entry_url, entry, requester, query)


    def _search_youtube_results(self, query: str) -> List[dict]:
        """用 yt-dlp 返回完整 YouTube 搜索结果。"""
        cache_key = query.strip().lower()
        with self.cache_lock:
            cached = self.search_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]

        search_query = f"ytsearch10:{query}"
        ydl_opts = {
            "quiet": True, "logger": DownloaderLog(),
            "no_warnings": True,
            "noprogress": True,
            "extract_flat": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)

        results = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            title = entry.get("title")
            entry_url = self._youtube_search_entry_url(entry)

            if not title or not entry_url:
                continue
            results.append(
                {
                    "title": title,
                    "url": entry_url,
                    "thumbnail": entry.get("thumbnail"),
                    "duration": entry.get("duration"),
                    "uploader": entry.get("uploader") or entry.get("channel"),
                }
            )
            if len(results) >= 10:
                break

        with self.cache_lock:
            if len(self.search_cache) >= 100:
                self.search_cache.pop(next(iter(self.search_cache)), None)
            self.search_cache[cache_key] = (time.monotonic(), results)
        return results


    def _search_youtube_choices(self, query: str) -> List[Choice]:
        """用 yt-dlp 返回 YouTube 搜索自动补全候选。"""
        choices = []
        for result in self._search_youtube_results(query):
            choices.append(
                Choice(
                    name=f"YouTube：{result['title']}"[:100],
                    value=result["url"][:100],
                )
            )
        return choices


    def _extract_collection_songs(self, url: str, requester) -> List[Song]:
        """提取播放列表/合集；如果不是多条目，则退化成单曲。"""
        if is_youtube_url(url) and "list=" in url:
            url = self._normalize_youtube_playlist_url(url)

        try:
            ydl_opts = self._build_ydl_opts(
                output_template=None,
                allow_playlist=True,
                extract_flat=True,
            )
            ydl_opts = self._apply_site_opts(url, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return self._parse_collection_info_to_songs(info, url, requester)
        except Exception:
            pass

        try:
            ydl_opts = {
                "quiet": True, "logger": DownloaderLog(),
                "no_warnings": True,
                "noprogress": True,
                "extract_flat": "in_playlist",
            }
            ydl_opts = self._apply_site_opts(url, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return self._parse_collection_info_to_songs(info, url, requester)
        except Exception as e:
            raise RuntimeError(f"Playlist extraction failed:{e}")


    def _parse_collection_info_to_songs(
        self,
        info: dict,
        fallback_url: str,
        requester,
    ) -> List[Song]:
        """把 yt-dlp 返回的列表信息转换成 Song 列表。"""
        songs = []
        entries = info.get("entries")

        if not entries:
            songs.append(self._create_song_from_info(fallback_url, info, requester, fallback_url))
            return songs

        parent_title = self._title_from_info(info, fallback_url, fallback_url)

        for entry in entries:
            if not entry:
                continue

            if is_bilibili_url(fallback_url):
                entry_url = (
                    entry.get("webpage_url")
                    or entry.get("original_url")
                    or entry.get("url")
                )
            else:
                entry_url = (
                    entry.get("url")
                    or entry.get("webpage_url")
                    or entry.get("original_url")
                )
            title = self._title_from_info(entry, parent_title, str(entry_url or fallback_url))
            if is_bilibili_url(str(entry_url or fallback_url)) and self._is_position_like_title(title):
                title = parent_title

            # 有些 flat entry 给的是 id，不是完整链接。
            if entry_url and not str(entry_url).startswith("http"):
                webpage_url = entry.get("webpage_url")
                if webpage_url and str(webpage_url).startswith("http"):
                    entry_url = webpage_url
                else:
                    ie_key = entry.get("ie_key", "")
                    if "youtube" in str(ie_key).lower():
                        entry_url = f"https://www.youtube.com/watch?v={entry_url}"
                    else:
                        continue

            if not entry_url:
                continue

            song = self._create_song_from_info(entry_url, entry, requester, title)
            song.title = self._clean_song_title(title, entry_url)
            songs.append(song)

        if not songs:
            songs.append(self._create_song_from_info(fallback_url, info, requester, fallback_url))

        return songs


    def _download_song(self, song: Song) -> Path:
        """Retry forbidden media URLs with fresh extraction and bounded backoff."""
        for attempt in range(3):
            if song.cancel_event.is_set() or self.close_event.is_set():
                raise RuntimeError("Download cancelled.")
            try:
                return self._download_attempt(song, attempt)
            except Exception as error:
                message = clean_media_error(error)
                if song.cancel_event.is_set() or self.close_event.is_set():
                    raise RuntimeError("Download cancelled.") from error
                if "403" not in message or attempt == 2:
                    raise RuntimeError(message + ("(Fresh extraction retries failed; skipping track.)" if "403" in message else "")) from error
                logger.debug("HTTP 403; fresh extraction retry %s/2: %s", attempt + 1, song.title)
                for _ in range(5 * (attempt + 1)):
                    if song.cancel_event.wait(0.2) or self.close_event.is_set():
                        raise RuntimeError("Download cancelled.")

    def _download_attempt(self, song: Song, attempt=0) -> Path:
        """下载线程拥有文件生命周期；取消时清理含分片在内的输出。"""
        unique_name = uuid.uuid4().hex
        self._add_download(unique_name)
        result = None
        def check_cancelled(_=None):
            if song.cancel_event.is_set() or self.close_event.is_set():
                raise RuntimeError("Download cancelled.")
        try:
            check_cancelled()
            opts = self._build_ydl_opts(str(self.cache_dir / f"{unique_name}.%(ext)s"))
            opts["cachedir"] = False
            opts["retries"] = 1
            opts["fragment_retries"] = 1
            opts["skip_unavailable_fragments"] = False
            if attempt == 2:
                opts["format"] = "bestaudio[protocol=https]/best[protocol=https]/bestaudio/best"
            opts["progress_hooks"] = [check_cancelled]
            opts["postprocessor_hooks"] = [check_cancelled]
            opts = self._apply_site_opts(song.input, opts)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(song.webpage_url or song.input, download=True)
                check_cancelled()
                result = self._locate_downloaded_file(ydl, info, unique_name)
            return result
        finally:
            self._remove_download(unique_name)
            if result is None or song.cancel_event.is_set() or self.close_event.is_set():
                for path in self.cache_dir.glob(f"{unique_name}.*"):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("无法删除下载残留: %s", path)


    def _locate_downloaded_file(self, ydl, info: dict, unique_name: str) -> Path:
        """根据 yt-dlp 输出结果定位最终下载文件。"""
        downloaded_path = Path(ydl.prepare_filename(info))
        final_path = downloaded_path.with_suffix(".mp3")

        if final_path.exists():
            return final_path

        if downloaded_path.exists():
            return downloaded_path

        candidates = [p for p in self.cache_dir.glob(f"{unique_name}.*") if p.suffix in {".mp3", ".m4a", ".webm", ".opus", ".ogg", ".mp4"} and p.stat().st_size > 0]
        if candidates:
            return candidates[0]

        raise FileNotFoundError("Download finished but the audio file is missing.")
