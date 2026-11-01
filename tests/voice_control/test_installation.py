"""Voice dependency installation and selected update environment."""
import unittest
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]

class VoiceInstallationTests(unittest.TestCase):
    def test_voice_installer_selects_active_update_environment(self):
        from scripts.install_voice import bot_python
        from bot_app.features.updates.engine import atomic_json
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            active = root / 'runtime/updates' / ('a' * 32) / 'environment' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            for path in (original, active):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            atomic_json(root / 'runtime/updates/active.json', {'python': active.relative_to(root).as_posix()})
            # Windows TEMP may use an 8.3 alias (RUNNER~1) while the updater
            # resolves the same directory to its long name (runneradmin).
            self.assertTrue(Path(bot_python(root)).samefile(active))


    def test_source_dependency_list_matches_voice_installer(self):
        from scripts.install_voice import DEPENDENCIES
        path = ROOT / 'requirements-voice.txt'
        if path.exists():
            requirements = tuple(line for line in path.read_text().splitlines() if line and not line.startswith('#'))
            self.assertEqual(DEPENDENCIES, requirements)

