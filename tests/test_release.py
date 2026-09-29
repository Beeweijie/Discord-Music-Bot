"""Verify that releases contain the application and exclude local state."""
import importlib.util
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
                for required in ("install.bat", "start.bat", ".env.example", "main.py",
                                 "bot_app/presentation/api/web/index.html", "scripts/setup_windows.ps1"):
                    self.assertIn(required, names)
                self.assertNotIn(".env", names)
                for name in names:
                    self.assertFalse(name.startswith(("data/", "runtime/", "logs/", "config/guilds/", ".venv/")), name)
                    self.assertNotIn("__pycache__", name)
                self.assertNotIn(b"\n", bundle.read("Discord-Music-Bot-v1.0.0-windows/install.bat").replace(b"\r\n", b""))
