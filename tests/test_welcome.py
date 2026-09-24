"""Offline checks for welcome events, permissions and Discord failures."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from bot.welcome import Welcome


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
