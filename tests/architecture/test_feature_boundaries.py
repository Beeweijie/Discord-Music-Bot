"""Feature ownership and compatibility contracts, independent of directory layers."""
import ast
import importlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom):
            yield node.module or ''
        elif isinstance(node, ast.Import):
            yield from (item.name for item in node.names)

class FeatureBoundaryTests(unittest.TestCase):
    def test_features_never_import_legacy_layers_or_application_composition(self):
        for path in (ROOT/'bot_app/features').rglob('*.py'):
            for name in imports(path):
                self.assertFalse(name.startswith(('bot_app.application','bot_app.domain','bot_app.infrastructure','bot_app.presentation','bot_app.app')), f'{path}: {name}')

    def test_cross_feature_dependencies_use_public_api(self):
        for path in (ROOT/'bot_app/features').rglob('*.py'):
            own = path.relative_to(ROOT/'bot_app/features').parts[0]
            for name in imports(path):
                if name.startswith('bot_app.features.'):
                    parts = name.split('.')
                    if parts[2] != own:
                        self.assertEqual(parts[3], 'api', f'{path}: {name}')

    def test_shared_modules_have_no_business_dependencies(self):
        for path in (ROOT/'bot_app/shared').rglob('*.py'):
            for name in imports(path):
                self.assertFalse(name.startswith(('bot_app.features','bot_app.app','bot_app.host')), f'{path}: {name}')

    def test_legacy_exports_are_the_same_classes(self):
        for legacy, canonical, name in [
            ('bot_app.application.music.service','bot_app.features.music.service','PlaybackService'),
            ('bot_app.presentation.discord.music','bot_app.features.music.adapters.discord','Music'),
            ('bot_app.infrastructure.updates','bot_app.features.updates.engine','GitHubUpdater'),
            ('bot_app.main','bot_app.app.main','MyBot')]:
            self.assertIs(getattr(importlib.import_module(legacy),name), getattr(importlib.import_module(canonical),name))

    def test_updater_imports_without_third_party_packages(self):
        import subprocess, sys
        code = "import sys; sys.path[:]=[p for p in sys.path if 'site-packages' not in p]; import bot_app.features.updates.engine"
        result = subprocess.run([sys.executable,'-S','-c',code],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
