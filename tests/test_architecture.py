"""Registration and architectural boundary checks; no Discord login."""
import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bot_app.presentation.discord.bot import create_bot
from bot_app.presentation.discord.music import Music
from bot_app.presentation.discord.panels import NowPlayingView

ROOT = Path(__file__).resolve().parent.parent
MUSIC_COMMANDS = {"join", "play", "queue", "pause", "resume", "now", "remove", "volume",
                  "seek", "shuffle", "skip", "stop", "help_music", "clear", "move", "loop", "replay", "fair", "dedupe", "nextup", "surprise", "sleep", "history", "favorite", "favorites", "unfavorite", "playlist_save", "playlist_load", "playlists", "playlist_delete"}


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = create_bot()
        await self.bot.__aenter__()
        self.addAsyncCleanup(self.bot.__aexit__, None, None, None)
        self.service = Mock()
        self.service.close = AsyncMock()
        self.service.dispatch = AsyncMock()
        with patch("bot_app.bootstrap.create_music_service", return_value=self.service):
            await self.bot.load_extension("bot_app.presentation.discord.music")
        await self.bot.load_extension("bot_app.presentation.discord.polls")
        self.cog = self.bot.get_cog("Music")

    async def test_all_original_command_names_and_aliases(self):
        prefix = set(self.bot.all_commands)
        self.assertEqual(prefix, MUSIC_COMMANDS | {"ping", "help", "sync", "a", "emoji", "add", "playlist_defult", "poll", "poll_end"})
        slash = {command.name for command in self.bot.tree.get_commands()}
        self.assertEqual(slash, MUSIC_COMMANDS | {"ping", "poll", "poll_end"})
        self.assertIs(self.bot.get_command("a"), self.bot.get_command("sync"))
        self.assertTrue(self.bot.get_command("sync").checks)

    async def test_default_playlist_command_is_guild_scoped_and_permission_checked(self):
        from types import SimpleNamespace
        command = self.bot.get_command("playlist_defult")
        self.assertEqual(len(command.checks), 2)
        management = Mock()
        management.get_settings.return_value = {"default_playlist_url": "old"}
        self.service.bot = SimpleNamespace(management=management)
        ctx = SimpleNamespace(guild=SimpleNamespace(id=123), send=AsyncMock(),
            author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)))
        from discord.ext.commands import MissingPermissions
        from discord.utils import maybe_coroutine
        with self.assertRaises(MissingPermissions):
            for check in command.checks:
                await maybe_coroutine(check, ctx)
        ctx.author.guild_permissions.manage_guild = True
        for check in command.checks:
            self.assertTrue(await maybe_coroutine(check, ctx))
        url = "https://music.youtube.com/playlist?list=custom"
        await command.callback(self.cog, ctx, url=url)
        management.update_settings.assert_called_once_with("music", {"default_playlist_url": url}, 123)

    async def test_prefix_and_slash_reach_same_use_case(self):
        ctx = Mock()
        await self.cog.skip_prefix.callback(self.cog, ctx)
        self.service.dispatch.assert_awaited_with("skip", ctx)
        interaction = Mock()
        interaction.response.defer = AsyncMock()
        await self.cog.skip_slash.callback(self.cog, interaction)
        self.assertEqual(self.service.dispatch.await_args.args[0], "skip")
        self.assertIs(self.service.dispatch.await_args.args[1].author, interaction.user)

    async def test_original_panel_buttons_and_autocomplete(self):
        self.service.sessions = {}
        panel = NowPlayingView(self.service, "1:2")
        self.assertEqual({child.label for child in panel.children},
                         {"Queue", "Pause", "Skip", "Loop: off", "Stop & clear", "Replay", "Add song", "Favorite", "Surprise next"})
        play = self.bot.tree.get_command("play")
        self.assertTrue(play.parameters[0].autocomplete)
        self.assertFalse(play.parameters[0].required)

    async def test_readme_support_columns_match_registered_commands(self):
        rows = {}
        for line in (ROOT / "README.md").read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("| `"):
                fields = line.split("|")
                rows[fields[1].strip().strip("`")] = fields[2].strip()
        self.assertEqual(set(rows), set(self.bot.all_commands))
        slash = {command.name for command in self.bot.tree.get_commands()}
        for name, support in rows.items():
            self.assertEqual(support, "both" if name in slash else "!")


class LayerTests(unittest.TestCase):
    def test_inward_dependency_boundaries(self):
        for layer in ("domain", "application"):
            for path in (ROOT / "bot_app" / layer).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    names = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                        [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                    for name in names:
                        self.assertFalse(name.startswith(("discord", "yt_dlp", "aiohttp", "tkinter",
                                                          "bot_app.infrastructure", "bot_app.presentation")),
                                         f"{path}: outward import {name}")
                        if layer == "domain":
                            self.assertFalse(name.startswith("bot_app.application"), str(path))

    def test_no_duplicate_methods_after_extraction(self):
        for path in (ROOT / "bot_app").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ClassDef):
                    methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    self.assertEqual(len(methods), len(set(methods)), f"{path}:{node.name}")
