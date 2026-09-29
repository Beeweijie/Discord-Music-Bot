"""Update trust boundaries, real process restarts and crash recovery; no login."""
import asyncio
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import URLError

from bot_app.application.updates import UpdateService
from bot_app.domain.updates import UpdateError, public_path, repository
from bot_app.infrastructure.updates import GitHubUpdater, REQUIRED, atomic_json, extract_release, read_json
from bot_app.infrastructure.updates import download
from scripts.run_bot import supervise

TAG = "v1.1.0"
REPO = "Beeweijie/Discord-Music-Bot"


def bundle(files=None):
    content = {name: b"# release\n" for name in REQUIRED}
    content.update({"bot_app/__init__.py": b"", "config/music.json": b'{"new": true}', "requirements.txt": b""})
    if files:
        content.update(files)
    content["version.json"] = json.dumps({"version": TAG, "update_schema": 1, "files": sorted(content)}).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, value in content.items():
            archive.writestr(f"Discord-Music-Bot-{TAG}-windows/{name}", value)
    return stream.getvalue()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.updater = GitHubUpdater(self.root)
        atomic_json(self.root / "version.json", {"version": "v1.0.0", "files": ["main.py", "bot_app/obsolete.py"]})
        for name, text in {"main.py": "# old launcher", "bot_app/obsolete.py": "# old module", ".env": "DISCORD_TOKEN=private",
                           "config/music.json": '{"local": true}', "config/guilds/123/music.json": '{"server": true}',
                           "data/library/123/456.json": '{"favorites": []}'}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.protected = {name: (self.root / name).read_bytes() for name in
                          (".env", "config/music.json", "config/guilds/123/music.json", "data/library/123/456.json")}

    def stage(self, files=None, real_environment=False, environment_error=None):
        payload = bundle(files)
        name = f"Discord-Music-Bot-{TAG}-windows.zip"
        digest = hashlib.sha256(payload).hexdigest()
        release = {"tag_name": TAG, "draft": False, "prerelease": False, "assets": [
            {"name": name, "state": "uploaded", "digest": "sha256:" + digest,
             "browser_download_url": f"https://github.com/{REPO}/releases/download/{TAG}/{name}"},
            {"name": name.replace(".zip", ".sha256"), "state": "uploaded",
             "browser_download_url": f"https://github.com/{REPO}/releases/download/{TAG}/{name.replace('.zip', '.sha256')}"}]}

        def environment(job):
            if environment_error:
                raise environment_error
            path = job / "environment" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if real_environment:
                subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(job / "environment")], check=True,
                               capture_output=True, timeout=90)
            else:
                path.parent.mkdir(parents=True)
                path.write_bytes(b"fake-python")
            return path

        with patch("bot_app.infrastructure.updates.download", side_effect=[json.dumps(release).encode(),
                   f"{digest}  {name}\n".encode(), payload]), patch.object(self.updater, "prepare_environment", side_effect=environment):
            return self.updater.prepare()

    def assert_preserved(self):
        for name, value in self.protected.items():
            self.assertEqual((self.root / name).read_bytes(), value, name)

    def test_only_configured_https_github_repository_is_allowed(self):
        self.assertEqual(repository(f"https://github.com/{REPO}/releases/latest"), REPO)
        for url in ("http://github.com/a/b", "https://github.com.evil/a/b", "https://user@github.com/a/b",
                    "https://github.com/a/b?token=secret", "https://github.com/a/b/releases/tag/v1.0.0"):
            with self.assertRaises(UpdateError):
                repository(url)
        with patch("bot_app.infrastructure.updates.download") as request:
            with self.assertRaises(UpdateError):
                self.updater.prepare("https://github.com/someone/another-repo")
            request.assert_not_called()

    def test_current_or_older_versions_do_not_download_assets(self):
        for tag in ("v1.0.0", "v0.9.0"):
            with patch("bot_app.infrastructure.updates.download", return_value=json.dumps({"tag_name": tag}).encode()) as request:
                self.assertIsNone(self.updater.prepare())
                self.assertEqual(request.call_count, 1)
        with patch("bot_app.infrastructure.updates.download", return_value=json.dumps({"tag_name": "v2.0.0", "prerelease": True}).encode()):
            with self.assertRaises(UpdateError):
                self.updater.prepare()

    def test_corrupt_checksum_aborts_before_creating_environment(self):
        archive = bundle()
        name = f"Discord-Music-Bot-{TAG}-windows"
        release = {"tag_name": TAG, "assets": [{"name": name + suffix, "state": "uploaded",
                   "browser_download_url": f"https://github.com/{REPO}/releases/download/{TAG}/{name}{suffix}"}
                   for suffix in (".zip", ".sha256")]}
        with patch("bot_app.infrastructure.updates.download", side_effect=[json.dumps(release).encode(),
                ("0" * 64 + "  " + name + ".zip\n").encode(), archive]), patch.object(self.updater, "prepare_environment") as install:
            with self.assertRaisesRegex(UpdateError, "checksum"):
                self.updater.prepare()
            install.assert_not_called()
        self.assertFalse(self.updater.pending.exists())
        self.assertEqual((self.root / "main.py").read_text(), "# old launcher")
        self.assert_preserved()

    def test_network_and_dependency_failures_leave_the_running_installation_unchanged(self):
        with patch("bot_app.infrastructure.updates.urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(UpdateError, "download"):
                download("https://api.github.com/repos/a/b/releases/latest", 1024)
        with self.assertRaisesRegex(UpdateError, "Dependencies"):
            self.stage(environment_error=UpdateError("Dependencies failed"))
        self.assertFalse(self.updater.pending.exists())
        self.assertEqual(self.updater.current_version(), "v1.0.0")
        self.assert_preserved()

    def test_zip_rejects_traversal_local_state_symlinks_and_case_duplicates(self):
        for name in ("../main.py", "C:/main.py", "bot_app/../../main.py", "bot_app/con.py", ".env",
                     "runtime/status.json", "config/guilds/123/music.json", "bot_app/file.py:stream"):
            self.assertFalse(public_path(name, config=True), name)
            archive = self.root / "bad.zip"
            archive.write_bytes(bundle({name: b"bad"}))
            with self.assertRaises(UpdateError):
                extract_release(archive, self.root / "extracted", TAG)
        for symlink in (True, False):
            archive = self.root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zip_file:
                name = f"Discord-Music-Bot-{TAG}-windows/bot_app/Test.py"
                info = zipfile.ZipInfo(name)
                if symlink:
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zip_file.writestr(info, b"bad")
                if not symlink:
                    zip_file.writestr(name.replace("Test.py", "test.py"), b"duplicate")
            with self.assertRaises(UpdateError):
                extract_release(archive, self.root / "extracted", TAG)

    def test_prepare_and_apply_preserve_local_files_and_remove_obsolete_code(self):
        ticket = self.stage()
        self.updater.activate(ticket, 123)
        self.assertEqual((self.root / "main.py").read_text(), "# old launcher")
        request = read_json(self.updater.pending)
        with patch("bot_app.infrastructure.updates.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            self.updater.apply(request)
        self.assertEqual(self.updater.current_version(), TAG)
        self.assertFalse((self.root / "bot_app/obsolete.py").exists())
        self.assert_preserved()
        self.updater.rollback(request)
        self.assertEqual(self.updater.current_version(), "v1.0.0")
        self.assertEqual((self.root / "bot_app/obsolete.py").read_text(), "# old module")
        self.assertFalse(self.updater.active.exists())
        self.assert_preserved()

    def test_configuration_failure_rolls_back_all_replaced_files(self):
        ticket = self.stage()
        self.updater.activate(ticket, 123)
        with patch("bot_app.infrastructure.updates.subprocess.run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(UpdateError):
                self.updater.apply(read_json(self.updater.pending))
        self.assertEqual(self.updater.current_version(), "v1.0.0")
        self.assertEqual((self.root / "main.py").read_text(), "# old launcher")
        self.assertFalse((self.root / "scripts/run_bot.py").exists())
        self.assert_preserved()

    def test_file_replacement_failure_rolls_back_previous_changes(self):
        ticket = self.stage()
        self.updater.activate(ticket, 123)
        original = self.updater.replace

        def fail_once(source, target):
            if source.parent.name == "source" and target.name == "main.py":
                raise PermissionError("locked file")
            return original(source, target)

        with patch.object(self.updater, "replace", side_effect=fail_once):
            with self.assertRaises(PermissionError):
                self.updater.apply(read_json(self.updater.pending))
        self.assertEqual(self.updater.current_version(), "v1.0.0")
        self.assertEqual((self.root / "bot_app/obsolete.py").read_text(), "# old module")
        self.assert_preserved()

    def test_real_supervisor_restarts_and_rolls_back_failed_startup(self):
        for mode in ("success", "exit", "timeout"):
            success = mode == "success"
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                original_root, original_updater = self.root, self.updater
                self.root = Path(directory).resolve()
                self.updater = GitHubUpdater(self.root)
                atomic_json(self.root / "version.json", {"version": "v1.0.0", "files": ["bot_app/main.py"]})
                old = '''import json, pathlib, sys
root = pathlib.Path.cwd()
if "--check" in sys.argv: raise SystemExit(0)
once = root / "runtime/once"
if not once.exists():
    once.write_text("1")
    request = json.loads((root / "runtime/request.json").read_text())
    (root / "runtime/updates/pending.json").write_text(json.dumps(request))
    raise SystemExit(75)
raise SystemExit(0)
'''
                (self.root / "bot_app").mkdir()
                (self.root / "bot_app/main.py").write_text(old)
                new = '''import json, os, pathlib, sys, time
root = pathlib.Path.cwd()
if "--check" in sys.argv: raise SystemExit(0)
if MODE == "timeout": time.sleep(10)
if not SUCCESS: raise SystemExit(42)
job = root / "runtime/updates" / os.environ["BOT_UPDATE_ID"]
(job / "ready.json").write_text("{}")
deadline = time.monotonic() + 10
while time.monotonic() < deadline and not (root / "runtime/updates/result.json").exists(): time.sleep(.05)
raise SystemExit(0)
'''.replace("SUCCESS", repr(success)).replace("MODE", repr(mode))
                try:
                    ticket = self.stage({"bot_app/main.py": new.encode()}, real_environment=True)
                    atomic_json(self.root / "runtime/request.json", {**ticket, "channel_id": 123})
                    self.assertEqual(supervise(self.root, [], readiness_timeout=.5 if mode == "timeout" else 10), 0)
                    result = read_json(self.updater.result)
                    self.assertEqual(result["success"], success)
                    self.assertEqual(self.updater.current_version(), TAG if success else "v1.0.0")
                    self.assertFalse(self.updater.pending.exists())
                finally:
                    self.root, self.updater = original_root, original_updater

    def test_interrupted_update_recovers_using_original_standalone_helper(self):
        (self.root / "bot_app/main.py").write_text("raise SystemExit(0)\n")
        ticket = self.stage()
        self.updater.activate(ticket, 123)
        with patch("bot_app.infrastructure.updates.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            self.updater.apply(read_json(self.updater.pending))
        self.assertEqual(supervise(self.root, []), 0)
        self.assertEqual(self.updater.current_version(), "v1.0.0")
        self.assertFalse(read_json(self.updater.result)["success"])
        self.assert_preserved()


class UpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_check_and_no_shutdown_on_failed_or_unnecessary_update(self):
        from bot_app.presentation.discord.bot import create_bot
        from discord.ext import commands
        bot = create_bot()
        bot.is_owner = AsyncMock(return_value=False)
        ctx = SimpleNamespace(bot=bot, author=SimpleNamespace(id=1), channel=SimpleNamespace(id=123), send=AsyncMock())
        command = bot.get_command("update")
        with self.assertRaises(commands.NotOwner):
            await command.checks[0](ctx)
        bot.is_owner.return_value = True
        self.assertTrue(await command.checks[0](ctx))
        bot.close = AsyncMock()
        bot.updates = Mock()
        bot.updates.prepare = AsyncMock(return_value=None)
        bot.updates.backend.current_version.return_value = TAG
        with patch.dict(os.environ, {"BOT_SUPERVISED": "1"}):
            await command.callback(ctx)
            bot.updates.prepare.side_effect = UpdateError("Bad checksum")
            await command.callback(ctx)
        bot.close.assert_not_awaited()
        bot.updates.activate.assert_not_called()

    async def test_verified_update_is_activated_before_graceful_shutdown(self):
        from bot_app.presentation.discord.bot import create_bot
        bot = create_bot()
        bot.close = AsyncMock()
        ticket = {"id": "a" * 32, "version": TAG}
        bot.updates = Mock()
        bot.updates.prepare = AsyncMock(return_value=ticket)
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
        with patch.dict(os.environ, {"BOT_SUPERVISED": "1"}):
            await bot.get_command("update").callback(ctx)
        bot.updates.activate.assert_called_once_with(ticket, 123)
        self.assertTrue(bot.update_requested)
        bot.close.assert_awaited_once()

    async def test_parallel_prepare_is_rejected(self):
        backend = Mock()
        service = UpdateService(backend)
        await service.lock.acquire()
        try:
            with self.assertRaises(UpdateError):
                await service.prepare()
            backend.prepare.assert_not_called()
        finally:
            service.lock.release()
