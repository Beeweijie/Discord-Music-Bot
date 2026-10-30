"""Offline checks for welcome events, permissions and Discord failures."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import discord

from bot.welcome import Welcome
from bot_app.app.management import ManagementService
from bot_app.app.configuration import JsonSettingsRepository
from bot_app.features.welcome.models import render_welcome_message


class WelcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {
            "WELCOME_ENABLED": "true",
            "WELCOME_INCLUDE_BOTS": "true",
            "WELCOME_CHANNEL_ID": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id = 10
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, send_messages=True,
        )
        self.channel.send = AsyncMock()
        self.guild = SimpleNamespace(
            id=1, name="Our **server** @everyone", member_count=12,
            system_channel=self.channel, me=object(),
            get_channel=Mock(return_value=None),
        )
        self.member = SimpleNamespace(
            id=2, guild=self.guild, mention="<@2>", pending=False, bot=False,
            joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.cog = Welcome(Mock())

    async def test_join_sends_safe_message_once(self):
        await self.cog.on_member_join(self.member)
        await self.cog.on_member_join(self.member)

        self.channel.send.assert_awaited_once()
        args, kwargs = self.channel.send.call_args
        self.assertIn("**12**", args[0])
        self.assertIn(r"Our \*\*server\*\*", args[0])
        self.assertNotIn("on_member_join", args[0])
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), {
            "parse": [], "users": [2],
        })

    async def test_waits_for_screening_then_sends_once(self):
        self.member.pending = True
        await self.cog.on_member_join(self.member)
        self.channel.send.assert_not_awaited()

        before = SimpleNamespace(pending=True)
        self.member.pending = False
        await self.cog.on_member_update(before, self.member)
        await self.cog.on_member_join(self.member)
        await self.cog.on_member_update(before, self.member)
        self.channel.send.assert_awaited_once()

    async def test_unrelated_member_updates_do_not_send(self):
        await self.cog.on_member_update(SimpleNamespace(pending=False), self.member)
        self.channel.send.assert_not_awaited()

    async def test_concurrent_events_do_not_duplicate_send(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def delayed_send(*args, **kwargs):
            started.set()
            await finish.wait()

        self.channel.send.side_effect = delayed_send
        first = asyncio.create_task(self.cog.on_member_join(self.member))
        await started.wait()
        try:
            await self.cog.on_member_join(self.member)
            self.channel.send.assert_awaited_once()
        finally:
            finish.set()
            await first
        self.assertFalse(self.cog._sending)

    async def test_http_failure_does_not_poison_retry(self):
        self.channel.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "no permission",
        )
        with self.assertLogs("bot.welcome", "WARNING"):
            await self.cog.on_member_join(self.member)
        self.assertFalse(self.cog._sending)
        self.assertFalse(self.cog._welcomed)
        self.channel.send.side_effect = None
        await self.cog.on_member_join(self.member)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_cancelled_send_releases_deduplication_marker(self):
        self.channel.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.cog.on_member_join(self.member)
        self.assertFalse(self.cog._sending)
        self.channel.send.side_effect = None
        await self.cog.on_member_join(self.member)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_missing_permissions_and_bot_member_are_safe(self):
        self.channel.permissions_for.return_value.send_messages = False
        with self.assertLogs("bot.welcome", "WARNING"):
            await self.cog.on_member_join(self.member)
        self.channel.send.assert_not_awaited()

        self.guild.me = None
        self.channel.permissions_for.reset_mock()
        with self.assertLogs("bot.welcome", "WARNING"):
            await self.cog.on_member_join(self.member)
        self.channel.permissions_for.assert_not_called()

    async def test_non_text_configured_channel_falls_back_to_system(self):
        self.guild.get_channel.return_value = Mock(spec=discord.CategoryChannel)
        await self.cog.on_member_join(self.member)
        self.channel.send.assert_awaited_once()

    async def test_no_usable_channel_is_safe(self):
        self.guild.system_channel = None
        await self.cog.on_member_join(self.member)
        self.channel.send.assert_not_awaited()

    async def test_configured_text_channel_is_used(self):
        preferred = Mock(spec=discord.TextChannel)
        preferred.id = 25
        preferred.permissions_for.return_value = SimpleNamespace(
            view_channel=True, send_messages=True,
        )
        preferred.send = AsyncMock()
        self.guild.get_channel.return_value = preferred
        with patch.dict("os.environ", {"WELCOME_CHANNEL_ID": "25"}):
            cog = Welcome(Mock())
        await cog.on_member_join(self.member)
        self.guild.get_channel.assert_called_once_with(25)
        preferred.send.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_disabled_and_bot_filter_settings(self):
        with patch.dict("os.environ", {"WELCOME_ENABLED": "false"}):
            cog = Welcome(Mock())
        await cog.on_member_join(self.member)
        self.channel.send.assert_not_awaited()

        with patch.dict("os.environ", {"WELCOME_INCLUDE_BOTS": "no"}):
            cog = Welcome(Mock())
        self.member.bot = True
        await cog.on_member_join(self.member)
        self.channel.send.assert_not_awaited()

    async def test_leave_and_rejoin_gets_another_welcome(self):
        await self.cog.on_member_join(self.member)
        await self.cog.on_member_remove(self.member)
        self.assertFalse(self.cog._welcomed)
        self.member.joined_at += timedelta(hours=1)
        await self.cog.on_member_join(self.member)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_rejoin_after_missed_remove_is_welcomed(self):
        await self.cog.on_member_join(self.member)
        self.member.joined_at += timedelta(hours=1)
        await self.cog.on_member_join(self.member)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_unknown_member_count_is_omitted(self):
        self.guild.member_count = None
        await self.cog.on_member_join(self.member)
        self.assertNotIn("None", self.channel.send.call_args.args[0])

    async def test_remembered_members_are_bounded_and_cleared_on_guild_remove(self):
        with patch("bot.welcome.MAX_REMEMBERED_WELCOMES", 2):
            for member_id in (2, 3, 4):
                self.member.id = member_id
                await self.cog.on_member_join(self.member)
        self.assertEqual(list(self.cog._welcomed), [(1, 3), (1, 4)])
        await self.cog.on_guild_remove(self.guild)
        self.assertFalse(self.cog._welcomed)

    def test_invalid_configuration_uses_defaults(self):
        with patch.dict("os.environ", {
            "WELCOME_ENABLED": "invalid", "WELCOME_CHANNEL_ID": "-1",
        }):
            with self.assertLogs("bot.welcome", "WARNING"):
                cog = Welcome(Mock())
        self.assertTrue(cog.enabled)
        self.assertEqual(cog.channel_id, 0)


if __name__ == "__main__":
    unittest.main()


class GuildWelcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_guild_welcome_preferences_do_not_leak(self):
        from bot_app.features.welcome.service import WelcomeService
        delivery = Mock()
        delivery.send = AsyncMock(return_value=True)
        service = WelcomeService(delivery, enabled=True, include_bots=True, channel_id=0, max_remembered=lambda: 100)
        repository = Mock()
        repository.read.side_effect = lambda section, guild: {"enabled": guild == 1, "include_bots": False, "channel_id": "11" if guild == 1 else "22"}
        service.settings_repository = repository
        for guild_id in (1, 2):
            member = SimpleNamespace(guild=SimpleNamespace(id=guild_id), id=3, joined_at=None, bot=False)
            await service._send_welcome(member)
        delivery.send.assert_awaited_once()
        self.assertEqual(delivery.pick_channel.call_args.args[1], 11)


class WelcomeCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repository = JsonSettingsRepository(self.root)
        self.management = ManagementService(Mock(), self.repository, Mock())
        self.management.catalog = lambda: [{"id": "1", "channels": [{"id": "10"}]}]
        self.bot = SimpleNamespace(management=self.management, intents=SimpleNamespace(members=True))
        self.cog = Welcome(self.bot)
        self.cog.settings_repository = self.repository
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id = 10
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
        self.channel.send = AsyncMock()
        self.guild = SimpleNamespace(id=1, name="Our **server**", member_count=12, me=object(),
                                     system_channel=self.channel,
                                     get_channel=lambda key: self.channel if key == 10 else None)
        self.member = SimpleNamespace(id=2, guild=self.guild, mention="<@2>", bot=False, pending=False,
                                      joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                      guild_permissions=SimpleNamespace(manage_guild=True))
        self.ctx = SimpleNamespace(guild=self.guild, channel=self.channel, author=self.member, send=AsyncMock())
        self.initial = {"enabled": False, "include_bots": False, "channel_id": "0", "message": "Hello {user}!"}
        self.repository.write("welcome", self.initial, 1)

    async def invoke(self, content=""):
        await self.cog.welcome_prefix.callback(self.cog, self.ctx, content=content)

    async def test_empty_command_binds_current_channel_preserves_template_and_enables(self):
        await self.invoke("   ")
        expected = dict(self.initial, enabled=True, channel_id="10")
        self.assertEqual(JsonSettingsRepository(self.root).read("welcome", 1), expected)
        self.channel.send.assert_not_awaited()
        await self.cog.on_member_join(self.member)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.call_args.args[0], "Hello <@2>!")

    async def test_custom_message_is_saved_per_server_and_rendered_with_safe_mentions(self):
        other = self.repository.read("welcome", 2)
        template = "Welcome {user} to {server}!\nMember {count}. @everyone <@&99> {unknown}"
        await self.invoke(template)
        self.assertEqual(self.repository.read("welcome", 1), dict(self.initial, enabled=True, channel_id="10", message=template))
        self.assertEqual(self.repository.read("welcome", 2), other)
        self.channel.send.assert_not_awaited()
        # Reload the service as after a restart.
        cog = Welcome(self.bot)
        cog.settings_repository = JsonSettingsRepository(self.root)
        await cog.on_member_join(self.member)
        text = self.channel.send.call_args.args[0]
        self.assertIn(r"Welcome <@2> to Our \*\*server\*\*!", text)
        self.assertIn("\nMember 12.", text)
        self.assertIn("{unknown}", text)
        self.assertEqual(self.channel.send.call_args.kwargs["allowed_mentions"].to_dict(), {"parse": [], "users": [2]})

    async def test_on_off_and_reset_preserve_unrelated_settings(self):
        await self.invoke("on")
        self.assertEqual(self.repository.read("welcome", 1), dict(self.initial, enabled=True))
        await self.invoke("off")
        self.assertEqual(self.repository.read("welcome", 1), self.initial)
        await self.invoke("reset")
        self.assertEqual(self.repository.read("welcome", 1), dict(self.initial, message=""))
        await self.invoke()
        await self.cog.on_member_join(self.member)
        self.assertIn("欢迎 <@2>", self.channel.send.call_args.args[0])

    async def test_status_displays_settings_without_changing_or_sending_welcome(self):
        await self.invoke("status")
        embed = self.ctx.send.call_args.kwargs["embed"]
        self.assertEqual(embed.description, self.initial["message"])
        self.assertEqual([field.value for field in embed.fields], ["Disabled", "<#10>", "Custom"])
        self.assertEqual(self.repository.read("welcome", 1), self.initial)
        self.channel.send.assert_not_awaited()

    async def test_preview_when_disabled_does_not_consume_real_welcome_or_change_settings(self):
        self.ctx.channel = Mock(spec=discord.TextChannel)
        self.ctx.channel.id = 20
        await self.invoke("test")
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.repository.read("welcome", 1), self.initial)
        self.assertFalse(self.cog._welcomed)
        self.assertFalse(self.cog._sending)
        await self.invoke("on")
        await self.cog.on_member_join(self.member)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_invalid_channel_permissions_and_message_do_not_overwrite_settings(self):
        for kwargs in ({"view_channel": False}, {"send_messages": False}):
            self.channel.permissions_for.return_value = SimpleNamespace(**dict(dict(view_channel=True, send_messages=True), **kwargs))
            await self.invoke()
            self.assertEqual(self.repository.read("welcome", 1), self.initial)
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
        await self.invoke("x" * 2001)
        self.assertEqual(self.repository.read("welcome", 1), self.initial)
        self.ctx.channel = Mock(spec=discord.Thread)
        await self.invoke()
        self.assertEqual(self.repository.read("welcome", 1), self.initial)
        self.channel.send.assert_not_awaited()

    async def test_missing_configured_channel_is_not_silently_replaced_for_on_or_test(self):
        saved = dict(self.initial, channel_id="99")
        self.repository.write("welcome", saved, 1)
        for action in ("on", "test"):
            await self.invoke(action)
            self.assertEqual(self.repository.read("welcome", 1), saved)
        self.channel.send.assert_not_awaited()

    async def test_missing_member_intent_reports_restart_but_off_remains_available(self):
        self.bot.intents.members = False
        await self.invoke()
        self.assertTrue(self.repository.read("welcome", 1)["enabled"])
        self.assertIn("Restart the bot", self.ctx.send.call_args.args[0])
        await self.invoke("off")
        self.assertFalse(self.repository.read("welcome", 1)["enabled"])

    async def test_command_requires_server_and_manage_server_permission(self):
        from discord.ext.commands import MissingPermissions, NoPrivateMessage
        from discord.utils import maybe_coroutine
        self.member.guild_permissions.manage_guild = False
        with self.assertRaises(MissingPermissions):
            for check in self.cog.welcome_prefix.checks:
                await maybe_coroutine(check, self.ctx)
        self.member.guild_permissions.manage_guild = True
        for check in self.cog.welcome_prefix.checks:
            self.assertTrue(await maybe_coroutine(check, self.ctx))
        self.ctx.guild = None
        with self.assertRaises(NoPrivateMessage):
            for check in self.cog.welcome_prefix.checks:
                await maybe_coroutine(check, self.ctx)

    async def test_failed_preview_reports_failure_without_changing_settings(self):
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        with self.assertLogs("bot.welcome", "WARNING"):
            await self.invoke("test")
        self.assertIn("Could not send", self.ctx.send.call_args.args[0])
        self.assertEqual(self.repository.read("welcome", 1), self.initial)


class WelcomeTemplateTests(unittest.TestCase):
    def test_replacements_are_literal_and_never_recursively_formatted(self):
        rendered = render_welcome_message("{user} {server} {count} {user.__class__}",
                                          user="<@2>", server="{user}", count=None)
        self.assertEqual(rendered, "<@2> {user} ? {user.__class__}")

    def test_expanded_message_fits_discord_limit_without_breaking_emoji(self):
        rendered = render_welcome_message("{server}" * 200, user="<@2>", server="🎉" * 100, count=12)
        self.assertEqual(len(rendered.encode("utf-16-le")), 4000)
        self.assertEqual(rendered, "🎉" * 1000)
