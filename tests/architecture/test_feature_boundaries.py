"""Enforce feature ownership and the minimal launcher surface."""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ('application', 'domain', 'infrastructure', 'presentation')


def imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom):
            yield node.module or ''
        elif isinstance(node, ast.Import):
            yield from (item.name for item in node.names)


class FeatureBoundaryTests(unittest.TestCase):
    def test_legacy_layer_trees_have_been_removed(self):
        for layer in LEGACY:
            self.assertFalse((ROOT / 'bot_app' / layer).exists(), layer)

    def test_features_do_not_import_composition_or_legacy_layers(self):
        forbidden = tuple('bot_app.' + layer for layer in LEGACY) + ('bot_app.app',)
        for path in (ROOT / 'bot_app/features').rglob('*.py'):
            for name in imports(path):
                self.assertFalse(name.startswith(forbidden), f'{path}: {name}')

    def test_cross_feature_imports_use_public_api(self):
        for path in (ROOT / 'bot_app/features').rglob('*.py'):
            own = path.relative_to(ROOT / 'bot_app/features').parts[0]
            for name in imports(path):
                if name.startswith('bot_app.features.'):
                    parts = name.split('.')
                    if parts[2] != own:
                        self.assertGreater(len(parts), 3, f'{path}: {name}')
                        self.assertEqual(parts[3], 'api', f'{path}: {name}')

    def test_shared_has_no_business_dependencies(self):
        for path in (ROOT / 'bot_app/shared').rglob('*.py'):
            for name in imports(path):
                self.assertFalse(name.startswith(('bot_app.features', 'bot_app.app', 'bot_app.host')), f'{path}: {name}')

    def test_updater_and_supervisor_need_only_standard_library(self):
        result = subprocess.run([sys.executable, '-S', '-c', 'import bot_app.features.updates.engine; import scripts.run_bot'],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chat_features_and_configuration_are_absent(self):
        from bot_app.app.settings_catalog import DEFAULTS
        self.assertEqual(set(DEFAULTS), {'music', 'welcome', 'voice_moderation'})
        for name in ('chat', 'chat_samples', 'memory', 'meme_memory', 'persona', 'surfing', 'vision', 'web_search'):
            self.assertFalse((ROOT / 'bot_app/features' / name).exists(), name)

    def test_stable_launcher_imports_resolve(self):
        from bot_app import main
        from bot_app.app.main import MyBot
        from bot.music import Music
        from bot_app.features.music.adapters.discord import Music as CanonicalMusic
        self.assertIs(main.MyBot, MyBot)
        self.assertIs(Music, CanonicalMusic)
