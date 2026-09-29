"""Trusted repository, version and public release path validation."""
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

DEFAULT_REPOSITORY = "https://github.com/Beeweijie/Discord-Music-Bot"
ROOT_FILES = {"README.md", "requirements.txt", ".env.example", "main.py", "install.bat", "start.bat", "version.json"}
TREE_EXTENSIONS = {"bot": {".py"}, "bot_app": {".py", ".html", ".css", ".js"},
                   "scripts": {".py", ".ps1", ".bat", ".vbs"}, "docs": {".md"}, "tests": {".py"}}
CONFIG_FILES = {"config/emoji.json", "config/music.json", "config/voice_moderation.json"}


class UpdateError(ValueError):
    """An update failed before committing; suitable for a public status message."""


def repository(url):
    parsed = urlsplit(url.strip())
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or parsed.query or parsed.fragment or len(parts) < 2
            or parts[2:] not in ([], ["releases"], ["releases", "latest"])
            or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."} for part in parts[:2])):
        raise UpdateError("Use an HTTPS GitHub repository URL, such as https://github.com/owner/repo.")
    return "/".join(parts[:2]).removesuffix(".git")


def version_tuple(tag):
    if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise UpdateError("Only stable releases with vMAJOR.MINOR.PATCH tags are supported.")
    return tuple(map(int, tag[1:].split(".")))


def public_path(name, *, config=False):
    """Reject traversal, Windows special paths, links and local-state destinations."""
    if not isinstance(name, str) or "\\" in name or ":" in name or "\0" in name:
        return False
    parts = name.split("/")
    reserved = {"con", "prn", "aux", "nul"} | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)}
    if any(not part or part in {".", "..", "__pycache__"} or part.endswith((".", " "))
           or part.split(".")[0].lower() in reserved for part in parts):
        return False
    path = PurePosixPath(name)
    return (name in ROOT_FILES or (config and name in CONFIG_FILES)
            or (len(parts) > 1 and parts[0] in TREE_EXTENSIONS
                and path.suffix in TREE_EXTENSIONS[parts[0]] and not any(part.startswith(".") for part in parts)))
