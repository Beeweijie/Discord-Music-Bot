"""Bounded GitHub downloads, staged environments and recoverable code updates.

This module uses only the standard library so a supervisor can restore a broken
release before importing any of its runtime dependencies.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bot_app.domain.updates import DEFAULT_REPOSITORY, UpdateError, public_path, repository, version_tuple

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
REQUIRED = {"main.py", "requirements.txt", "version.json", "scripts/run_bot.py", "bot_app/main.py",
            "bot_app/infrastructure/updates.py", "bot_app/domain/updates.py"}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def safe_target(root, name):
    if not public_path(name):
        raise UpdateError("The release contains a protected or invalid file path.")
    root = root.resolve()
    path = root / name
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise UpdateError("Update destinations must not contain symbolic links or junctions.")
    if not path.resolve().is_relative_to(root):
        raise UpdateError("An update path escapes the installation folder.")
    return path


def download(url, limit):
    accept = "application/vnd.github+json" if url.startswith("https://api.github.com/") else "application/octet-stream"
    request = Request(url, headers={"User-Agent": "Discord-Music-Bot-Updater", "Accept": accept})
    try:
        with urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
    except HTTPError as exc:
        raise UpdateError(f"GitHub returned HTTP {exc.code}. Check the public repository and API rate limit, then retry.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise UpdateError("Could not download the update from GitHub. The running version is unchanged.") from exc
    if len(data) > limit:
        raise UpdateError("The release download exceeds the allowed size.")
    return data


def extract_release(archive, destination, tag):
    prefix = f"Discord-Music-Bot-{tag}-windows/"
    seen = set()
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 2000:
            raise UpdateError("The release contains too many files.")
        for info in entries:
            if not info.filename.startswith(prefix):
                raise UpdateError("Unexpected release archive layout.")
            name = info.filename[len(prefix):]
            if (info.is_dir() or not public_path(name, config=True)
                    or stat.S_ISLNK(info.external_attr >> 16) or name.lower() in seen):
                raise UpdateError("Unsafe or duplicate path in the release archive.")
            seen.add(name.lower())
            total += info.file_size
            if total > MAX_EXPANDED or info.flag_bits & 1:
                raise UpdateError("The release archive is oversized or encrypted.")
        for info in entries:
            name = info.filename[len(prefix):]
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bundle.read(info))
    metadata = read_json(destination / "version.json")
    if (not isinstance(metadata, dict) or metadata.get("version") != tag or metadata.get("update_schema") != 1
            or not isinstance(metadata.get("files"), list)):
        raise UpdateError("The release is missing a compatible update manifest.")
    files = metadata["files"]
    actual = {str(path.relative_to(destination).as_posix()) for path in destination.rglob("*") if path.is_file()}
    if (not all(isinstance(name, str) and public_path(name, config=True) for name in files)
            or set(files) != actual or len(files) != len(actual) or not REQUIRED <= actual):
        raise UpdateError("The release manifest does not match its files.")
    return sorted(name for name in files if public_path(name))


class GitHubUpdater:
    def __init__(self, root, url=None):
        self.root = Path(root).resolve()
        self.repository = repository(url or os.getenv("UPDATE_GITHUB_URL", DEFAULT_REPOSITORY))
        self.directory = self.root / "runtime" / "updates"
        if not self.directory.resolve().is_relative_to(self.root):
            raise UpdateError("The runtime directory must be inside the installation folder.")
        self.pending = self.directory / "pending.json"
        self.active = self.directory / "active.json"
        self.result = self.directory / "result.json"

    def current_version(self):
        value = read_json(self.root / "version.json", {})
        return value.get("version", "v0.0.0")

    def prepare(self, url=""):
        if url and repository(url).lower() != self.repository.lower():
            raise UpdateError("That URL does not match UPDATE_GITHUB_URL. Configure the trusted repository locally first.")
        if self.pending.exists():
            raise UpdateError("Another update is waiting for restart. Check logs/update.log.")
        release = json.loads(download(f"https://api.github.com/repos/{self.repository}/releases/latest", 2 * 1024 * 1024))
        tag = release.get("tag_name")
        if release.get("draft") or release.get("prerelease"):
            raise UpdateError("Only published stable releases can be installed.")
        if version_tuple(tag) <= version_tuple(self.current_version()):
            return None
        base = f"Discord-Music-Bot-{tag}-windows"
        assets = {asset.get("name"): asset for asset in release.get("assets", []) if asset.get("state") == "uploaded"}
        data = {}
        for extension, limit in (("sha256", 4096), ("zip", MAX_ARCHIVE)):
            name = f"{base}.{extension}"
            asset = assets.get(name, {})
            url = asset.get("browser_download_url", "")
            expected = f"https://github.com/{self.repository}/releases/download/{tag}/{name}"
            if url.lower() != expected.lower():
                raise UpdateError("The latest release is missing the expected Windows ZIP or SHA-256 asset. Retry after publishing finishes.")
            data[extension] = download(url, limit)
        checksum = re.fullmatch(rb"([a-fA-F0-9]{64})  " + re.escape((base + ".zip").encode()) + rb"\s*", data["sha256"])
        digest = hashlib.sha256(data["zip"]).hexdigest()
        github_digest = assets[base + ".zip"].get("digest")
        if not checksum or checksum[1].decode().lower() != digest or (github_digest and github_digest != "sha256:" + digest):
            raise UpdateError("The release checksum does not match. No installed files were changed.")
        job_id = uuid.uuid4().hex
        job = self.directory / job_id
        job.mkdir(parents=True)
        archive = job / "release.zip"
        archive.write_bytes(data["zip"])
        try:
            files = extract_release(archive, job / "source", tag)
            for name in files:
                safe_target(self.root, name)
            python = self.prepare_environment(job)
            # Keep a standalone copy of the currently working updater. It can
            # repair a crash halfway through replacing the implementation files.
            domain = Path(__file__).parents[1] / "domain/updates.py"
            helper = domain.read_text(encoding="utf-8") + "\n" + Path(__file__).read_text(encoding="utf-8").replace(
                "from bot_app.domain.updates import DEFAULT_REPOSITORY, UpdateError, public_path, repository, version_tuple", "")
            compile(helper, str(job / "recovery.py"), "exec")
            (job / "recovery.py").write_text(helper, encoding="utf-8")
            atomic_json(job / "plan.json", {"id": job_id, "version": tag, "files": files,
                        "python": str(python.relative_to(self.root).as_posix())})
            return {"id": job_id, "version": tag}
        except (OSError, ValueError, SyntaxError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("Update preparation failed. The running version is unchanged; see runtime/updates for details.") from exc

    def prepare_environment(self, job):
        environment = job / "environment"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        clean_environment = os.environ.copy()
        clean_environment.pop("PYTHONPATH", None)
        clean_environment.pop("PYTHONHOME", None)
        with (job / "prepare.log").open("w", encoding="utf-8") as log:
            for command, timeout in (([sys.executable, "-m", "venv", str(environment)], 90),
                    ([str(python), "-m", "pip", "install", "-r", str(job / "source/requirements.txt")], 600),
                    ([str(python), "-c", "import bot_app.main; import bot_app.infrastructure.updates; import scripts.run_bot; import bot_app.presentation.api.server" +
                     ("; import tkinter, pystray" if os.name == "nt" else "")], 60)):
                completed = subprocess.run(command, cwd=job / "source", stdout=log, stderr=subprocess.STDOUT,
                    timeout=timeout, env=clean_environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if completed.returncode:
                    raise UpdateError("New-version dependencies or imports failed. The running bot is unchanged; see runtime/updates/*/prepare.log.")
        return python

    def job(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise UpdateError("Invalid update job identity.")
        path = self.directory / job_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.directory.resolve()):
            raise UpdateError("Invalid update job path.")
        return path

    def activate(self, ticket, channel_id):
        plan = read_json(self.job(ticket["id"]) / "plan.json")
        if not plan or plan["version"] != ticket["version"] or self.pending.exists():
            raise UpdateError("The prepared update is no longer available.")
        atomic_json(self.pending, {"id": ticket["id"], "channel_id": int(channel_id), "version": ticket["version"]})

    def cancel(self):
        self.pending.unlink(missing_ok=True)

    def selected_python(self, fallback):
        active = read_json(self.active)
        if not active:
            return fallback
        return str(self.environment_python(active["python"]))

    def environment_python(self, name):
        path = self.root / name
        expected = self.directory.resolve()
        if not path.resolve().is_relative_to(expected) or not path.is_file():
            raise UpdateError("The prepared Python environment is missing or outside the update directory.")
        return path

    def apply(self, request):
        job = self.job(request["id"])
        plan = read_json(job / "plan.json")
        if not plan or plan["version"] != request["version"]:
            raise UpdateError("The prepared release does not match the restart request.")
        self.environment_python(plan["python"])
        files = plan["files"]
        if not isinstance(files, list) or not REQUIRED <= set(files) or not all(public_path(name) for name in files):
            raise UpdateError("Invalid prepared update file list.")
        old = read_json(self.root / "version.json", {}).get("files", [])
        changes = sorted(set(files) | {name for name in old if public_path(name)})
        originals = []
        for name in changes:
            target = safe_target(self.root, name)
            if target.exists() and not target.is_file():
                raise UpdateError("An update destination is not a regular file.")
            originals.append({"path": name, "existed": target.exists()})
            if target.exists():
                backup = job / "backup" / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
        journal = {"phase": "applying", "originals": originals, "old_active": read_json(self.active)}
        atomic_json(job / "journal.json", journal)
        try:
            for name in changes:
                target = safe_target(self.root, name)
                if name in files:
                    self.replace(job / "source" / name, target)
                else:
                    target.unlink(missing_ok=True)
            python = self.environment_python(plan["python"])
            with (job / "check.log").open("w", encoding="utf-8") as log:
                checked = subprocess.run([str(python), "-m", "bot_app.main", "--check"], cwd=self.root,
                    stdout=log, stderr=subprocess.STDOUT, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if checked.returncode:
                raise UpdateError("The installed release failed its configuration check.")
            atomic_json(self.active, {"python": plan["python"], "version": plan["version"]})
            journal["phase"] = "applied"
            atomic_json(job / "journal.json", journal)
            return str(python)
        except Exception:
            self.rollback(request)
            raise

    @staticmethod
    def replace(source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".update.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def rollback(self, request):
        job = self.job(request["id"])
        journal = read_json(job / "journal.json")
        if not journal or journal["phase"] in {"rolled_back", "committed"}:
            return
        for entry in journal["originals"]:
            target = safe_target(self.root, entry["path"])
            if entry["existed"]:
                self.replace(job / "backup" / entry["path"], target)
            else:
                target.unlink(missing_ok=True)
        if journal["old_active"]:
            atomic_json(self.active, journal["old_active"])
        else:
            self.active.unlink(missing_ok=True)
        journal["phase"] = "rolled_back"
        atomic_json(job / "journal.json", journal)

    def finish(self, request, success):
        if success:
            job = self.job(request["id"])
            journal = read_json(job / "journal.json")
            journal["phase"] = "committed"
            atomic_json(job / "journal.json", journal)
        atomic_json(self.result, {"channel_id": request["channel_id"], "version": request["version"], "success": success})
        self.cancel()

    def mark_ready(self, job_id):
        job = self.job(job_id)
        plan = read_json(job / "plan.json")
        if plan and plan["version"] == self.current_version():
            atomic_json(job / "ready.json", {"ready": True})
