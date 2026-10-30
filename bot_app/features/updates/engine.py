"""Bounded GitHub downloads, staged environments and recoverable code updates.

This module uses only the standard library so a supervisor can restore a broken
release before importing any of its runtime dependencies.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote

from bot_app.features.updates.policies import DEFAULT_REPOSITORY, UpdateError, public_path, repository, version_tuple, update_target

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


def extract_source(archive, destination, tree, source):
    """Verify selected source blobs against the pinned commit's Git tree before writing."""
    if tree.get('truncated') is not False or not isinstance(tree.get('tree'), list):
        raise UpdateError('GitHub returned an incomplete source tree.')
    expected = {}
    for entry in tree['tree']:
        name = entry.get('path')
        if public_path(name):
            if (entry.get('type') != 'blob' or entry.get('mode') not in {'100644','100755'}
                    or not re.fullmatch(r'[a-f0-9]{40}', str(entry.get('sha', '')))):
                raise UpdateError('Source application files must be regular Git blobs.')
            expected[name] = entry['sha']
    if not REQUIRED <= expected.keys() or len(expected) > 2000:
        raise UpdateError('The source commit lacks required application files or is too large.')
    selected, seen, prefix, total = {}, set(), None, 0
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 10000:
            raise UpdateError('The source archive contains too many entries.')
        for info in entries:
            parts = info.filename.rstrip('/').split('/')
            if (not parts or any(not p or p in {'.','..'} or '\\' in p or ':' in p or '\0' in p for p in parts)
                    or not re.fullmatch(r'[A-Za-z0-9_.-]+',parts[0])
                    or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                raise UpdateError('Unsafe path or link in the source archive.')
            if prefix is None:
                prefix = parts[0]
            if prefix != parts[0] or info.filename.lower().rstrip('/') in seen:
                raise UpdateError('Unexpected root or duplicate source archive path.')
            seen.add(info.filename.lower().rstrip('/'))
            total += info.file_size
            if total > MAX_EXPANDED:
                raise UpdateError('The source archive exceeds the size limit.')
            if info.is_dir():
                continue
            name = '/'.join(parts[1:])
            if name in expected:
                content = bundle.read(info)
                digest = hashlib.sha1(b'blob ' + str(len(content)).encode('ascii') + b'\0' + content).hexdigest()
                if digest != expected[name]:
                    raise UpdateError('A source blob does not match the pinned Git commit.')
                selected[name] = content
    if selected.keys() != expected.keys():
        raise UpdateError('The source archive is missing expected Git blobs.')
    metadata = json.loads(selected['version.json'].decode('utf-8-sig'))
    if metadata.get('update_schema') != 1:
        raise UpdateError('This commit does not support the installed update protocol.')
    version_tuple(metadata.get('version'))
    metadata.update(files=sorted(selected), source=source)
    selected['version.json'] = json.dumps(metadata,indent=2).encode('utf-8')
    for name, content in selected.items():
        path = destination / name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(content)
    return sorted(selected)


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

    def current_label(self):
        metadata = read_json(self.root / 'version.json', {})
        return self.version_label(metadata.get('version', 'v0.0.0'), metadata.get('source'))

    @staticmethod
    def version_label(version, source=None):
        if source:
            return f"{version} ({source['kind']}:{source['ref']} @ {source['commit'][:12]})"
        return version

    def prepare(self, target=""):
        kind, ref = update_target(target, self.repository)
        if self.pending.exists():
            raise UpdateError("Another update is waiting for restart. Check logs/update.log.")
        try:
            return self._prepare_release() if kind == 'release' else self._prepare_source(kind,ref)
        except UpdateError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise UpdateError('GitHub returned invalid update metadata. No installed files were changed.') from error

    def _prepare_release(self):
        release = json.loads(download(f"https://api.github.com/repos/{self.repository}/releases/latest", 2 * 1024 * 1024))
        tag = release.get("tag_name")
        if release.get("draft") or release.get("prerelease"):
            raise UpdateError("Only published stable releases can be installed.")
        installed = read_json(self.root / 'version.json', {})
        current = version_tuple(self.current_version())
        if version_tuple(tag) < current or (version_tuple(tag) == current and not installed.get('source')):
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
        return self._stage(data['zip'], tag=tag)

    def _prepare_source(self, kind, ref):
        api = f'https://api.github.com/repos/{self.repository}'
        if kind == 'branch':
            branch = json.loads(download(api + '/branches/' + quote(ref,safe=''), 2 * 1024 * 1024))
            commit = branch['commit']
        else:
            commits = json.loads(download(api + '/commits?sha=' + quote(ref,safe='') + '&per_page=1', 2 * 1024 * 1024))
            if not isinstance(commits,list) or len(commits) != 1:
                raise UpdateError('The requested commit could not be resolved unambiguously.')
            commit = commits[0]
        sha, tree_sha = commit['sha'], commit['commit']['tree']['sha']
        if (not re.fullmatch(r'[a-f0-9]{40}', str(sha)) or not re.fullmatch(r'[a-f0-9]{40}', str(tree_sha))
                or (kind == 'commit' and not sha.startswith(ref))):
            raise UpdateError('GitHub did not resolve the requested immutable commit.')
        source = {'kind':kind, 'ref':ref, 'commit':sha, 'repository':self.repository}
        installed = read_json(self.root / 'version.json', {}).get('source') or {}
        if installed.get('commit') == sha and installed.get('repository','').lower() == self.repository.lower():
            return None
        tree = json.loads(download(api + '/git/trees/' + tree_sha + '?recursive=1', 4 * 1024 * 1024))
        if tree.get('sha') != tree_sha:
            raise UpdateError('GitHub returned a different source tree.')
        payload = download(f'https://codeload.github.com/{self.repository}/zip/{sha}', MAX_ARCHIVE)
        return self._stage(payload, source=source, tree=tree)

    def _stage(self, payload, *, tag=None, source=None, tree=None):
        job_id = uuid.uuid4().hex
        job = self.directory / job_id
        job.mkdir(parents=True)
        archive = job / "release.zip"
        archive.write_bytes(payload)
        try:
            files = (extract_source(archive, job / 'source', tree, source) if source
                     else extract_release(archive, job / 'source', tag))
            tag = read_json(job / 'source/version.json')['version']
            label = self.version_label(tag, source)
            for name in files:
                safe_target(self.root, name)
            self.check_memory_schema(job / 'source')
            python = self.prepare_environment(job)
            # Keep a standalone copy of the currently working updater. It can
            # repair a crash halfway through replacing the implementation files.
            domain = Path(__file__).with_name("policies.py")
            helper = domain.read_text(encoding="utf-8") + "\n" + re.sub(
                r'^from bot_app\.features\.updates\.policies import .*$', '',
                Path(__file__).read_text(encoding='utf-8'), flags=re.MULTILINE)
            compile(helper, str(job / "recovery.py"), "exec")
            (job / "recovery.py").write_text(helper, encoding="utf-8")
            atomic_json(job / "plan.json", {"id": job_id, "version": tag, "label":label, "source":source, "files": files,
                        "python": str(python.relative_to(self.root).as_posix())})
            return {"id": job_id, "version": tag, "label":label}
        except (OSError, ValueError, SyntaxError, sqlite3.Error, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("Update preparation failed. The running version is unchanged; see runtime/updates for details.") from exc

    def prepare_environment(self, job):
        environment = job / "environment"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        clean_environment = os.environ.copy()
        clean_environment.pop("PYTHONPATH", None)
        clean_environment.pop("PYTHONHOME", None)
        commands = [([sys.executable, "-m", "venv", str(environment)], 90),
                    ([str(python), "-m", "pip", "install", "-r", str(job / "source/requirements.txt")], 600)]
        voice_installer = job / 'source/scripts/install_voice.py'
        if (os.getenv('VOICE_COMMANDS_ENABLED', 'false').lower() in {'1', 'true', 'yes', 'on'}
                and voice_installer.is_file()):
            commands.append(([str(python), str(voice_installer)], 600))
        commands.append(([str(python), "-c", "import bot_app.main; import bot_app.infrastructure.updates; import scripts.run_bot; import bot_app.presentation.api.server" +
                         ("; import tkinter, pystray" if os.name == "nt" else "")], 60))
        with (job / "prepare.log").open("w", encoding="utf-8") as log:
            for command, timeout in commands:
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
        atomic_json(self.pending, {"id": ticket["id"], "channel_id": int(channel_id), "version": ticket["version"],
                                  "label":plan.get('label',plan['version'])})

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
        # POSIX venv executables normally link to the system Python. Resolve the
        # containing directory to reject escaping directories, but launch the
        # executable through its venv path so Python uses that environment.
        if not path.parent.resolve().is_relative_to(expected) or not path.is_file():
            raise UpdateError("The prepared Python environment is missing or outside the update directory.")
        return path

    def data_path(self, name):
        path = self.root / name
        if not path.resolve().is_relative_to(self.root):
            raise UpdateError('AI database paths must remain inside the installation.')
        for parent in (path,*path.parents):
            if parent == self.root:
                break
            if parent.is_symlink() or (hasattr(parent,'is_junction') and parent.is_junction()):
                raise UpdateError('AI database paths must not contain links or junctions.')
        return path

    def check_memory_schema(self, source):
        schemas = read_json(source / 'version.json', {}).get('data_schemas', {})
        if not isinstance(schemas,dict):
            raise UpdateError('Invalid data schema declaration in the update.')
        expected = schemas.get('memory')
        if expected is None:  # Older versions do not use the new memory store.
            return
        if type(expected) is not int or expected < 1:
            raise UpdateError('Invalid memory schema declaration in the update.')
        path = self.data_path('data/memory/memory.sqlite3')
        if not path.exists():
            return
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro',uri=True,timeout=5)) as db:
            rows = db.execute('SELECT version FROM meta').fetchall()
        if rows != [(expected,)]:
            raise UpdateError('Memory database format is incompatible with this version. No data was changed; an explicit migration is required.')

    def backup_ai_data(self, job):
        """The bot has stopped; retain a recoverable snapshot without replacing live data."""
        for name in ('data/memory/memory.sqlite3','data/chat/chat.sqlite3','data/ai/usage.sqlite3'):
            path = self.data_path(name)
            if not path.exists():
                continue
            backup = job / 'data-backup' / name
            backup.parent.mkdir(parents=True,exist_ok=True)
            temporary = backup.with_suffix('.sqlite3.tmp')
            try:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro',uri=True,timeout=5)) as original, \
                     closing(sqlite3.connect(temporary)) as copied:
                    original.backup(copied)
                os.replace(temporary,backup)
            except (sqlite3.Error,OSError) as error:
                raise UpdateError('Could not back up the AI databases. No application files were replaced.') from error
            finally:
                temporary.unlink(missing_ok=True)

    def apply(self, request):
        job = self.job(request["id"])
        plan = read_json(job / "plan.json")
        if not plan or plan["version"] != request["version"]:
            raise UpdateError("The prepared release does not match the restart request.")
        self.environment_python(plan["python"])
        files = plan["files"]
        if not isinstance(files, list) or not REQUIRED <= set(files) or not all(public_path(name) for name in files):
            raise UpdateError("Invalid prepared update file list.")
        self.check_memory_schema(job / 'source')
        self.backup_ai_data(job)
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
            atomic_json(self.active, {"python": plan["python"], "version": plan["version"], "source":plan.get('source')})
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
        atomic_json(self.result, {"channel_id": request["channel_id"], "version": request["version"],
                                 "label":request.get('label',request['version']), "success": success})
        self.cancel()

    def mark_ready(self, job_id):
        job = self.job(job_id)
        plan = read_json(job / "plan.json")
        metadata = read_json(self.root / 'version.json', {})
        expected = (plan or {}).get('source') or {}
        installed = metadata.get('source') or {}
        if (plan and plan["version"] == self.current_version()
                and expected.get('commit') == installed.get('commit')):
            atomic_json(job / "ready.json", {"ready": True})
