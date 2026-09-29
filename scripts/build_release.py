"""Build a Windows source distribution from an explicit public-file allowlist."""
import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = ("README.md", "requirements.txt", ".env.example", "main.py", "install.bat", "start.bat", "version.json")
CONFIG_FILES = ("emoji.json", "music.json", "voice_moderation.json")
TREES = {
    "bot": {".py"},
    "bot_app": {".py", ".html", ".css", ".js"},
    "scripts": {".py", ".ps1", ".bat", ".vbs"},
    "docs": {".md"},
    "tests": {".py"},
}


def release_files():
    files = [ROOT / name for name in ROOT_FILES]
    files += [ROOT / "config" / name for name in CONFIG_FILES]
    for directory, extensions in TREES.items():
        files.extend(path for path in (ROOT / directory).rglob("*")
                     if path.is_file() and path.suffix in extensions
                     and "__pycache__" not in path.parts)
    return sorted(files)


def build(version, output):
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("Use a version such as v1.0.0")
    output.mkdir(parents=True, exist_ok=True)
    name = f"Discord-Music-Bot-{version}-windows"
    archive = output / f"{name}.zip"
    files = release_files()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            info = zipfile.ZipInfo(f"{name}/{path.relative_to(ROOT).as_posix()}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            data = path.read_bytes()
            if path.name == "version.json":
                data = json.dumps({"version": version, "update_schema": 1,
                                   "files": [item.relative_to(ROOT).as_posix() for item in files]}, indent=2).encode("utf-8")
            if path.suffix in {".bat", ".ps1", ".vbs"}:
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            bundle.writestr(info, data)
    checksum = output / f"{name}.sha256"
    checksum.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n", encoding="ascii")
    print(archive)
    print(checksum)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(args.version, args.output)
