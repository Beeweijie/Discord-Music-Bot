"""Verify that releases contain the application and exclude local state."""
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_release", ROOT / "scripts/build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_distribution_is_complete_and_has_no_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = release.build("v1.0.0", Path(directory))
            first = archive.read_bytes()
            self.assertEqual(first, release.build("v1.0.0", Path(directory)).read_bytes())
            with zipfile.ZipFile(archive) as bundle:
                names = {name.split("/", 1)[1] for name in bundle.namelist()}
                metadata = json.loads(bundle.read("Discord-Music-Bot-v1.0.0-windows/version.json"))
                self.assertEqual(metadata["version"], "v1.0.0")
                self.assertEqual(metadata["update_schema"], 1)
                self.assertEqual(set(metadata["files"]), names)
                for required in ("install.bat", "start.bat", ".env.example", "main.py",
                                 "bot_app/presentation/api/web/index.html", "scripts/setup_windows.ps1",
                                 "scripts/install_voice.py", "bot_app/infrastructure/media/voice_assets.py"):
                    self.assertIn(required, names)
                self.assertNotIn(".env", names)
                for name in names:
                    self.assertFalse(name.startswith(("data/", "runtime/", "logs/", "config/guilds/", ".venv/")), name)
                    self.assertNotIn("__pycache__", name)
                self.assertNotIn(b"\n", bundle.read("Discord-Music-Bot-v1.0.0-windows/install.bat").replace(b"\r\n", b""))
                # Existing v1.3.0 update clients have this unchanged allowlist.
                from bot_app.domain.updates import public_path
                self.assertTrue(all(public_path(name, config=True) for name in names))
                self.assertNotIn('requirements-voice.txt', names)
                self.assertFalse(any(name.endswith('.wav') for name in names))

    def test_voice_installer_selects_active_update_environment(self):
        from scripts.install_voice import bot_python
        from bot_app.infrastructure.updates import atomic_json
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            active = root / 'runtime/updates' / ('a' * 32) / 'environment' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            for path in (original, active):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            atomic_json(root / 'runtime/updates/active.json', {'python': active.relative_to(root).as_posix()})
            self.assertEqual(Path(bot_python(root)), active)

    def test_source_dependency_list_matches_voice_installer(self):
        from scripts.install_voice import DEPENDENCIES
        path = ROOT / 'requirements-voice.txt'
        if path.exists():
            requirements = tuple(line for line in path.read_text().splitlines() if line and not line.startswith('#'))
            self.assertEqual(DEPENDENCIES, requirements)
