import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import discord
from bot_app.domain.polls import validate_poll, poll_message_target
from bot_app.infrastructure.persistence.polls import PollRepository
from bot_app.presentation.discord.polls import Polls


class PollValidationTests(unittest.TestCase):
    def test_limits_whitespace_and_multiselect(self):
        value = validate_poll(' Question ', ' Yes | No ', 768, True)
        self.assertEqual(value, dict(question='Question', answers=['Yes', 'No'], hours=768, multiple=True))
        for args in [('', 'A|B'), ('Q'*301, 'A|B'), ('Q', 'A'), ('Q', 'A||B'), ('Q', 'A|a'),
                     ('Q', 'A|'+'B'*56), ('Q', '|'.join(map(str, range(11)))),
                     ('Q', 'A|B', 0), ('Q', 'A|B', 769), ('Q', 'A|B', True)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_poll(*args)

    def test_link_scope_and_id_validation(self):
        self.assertEqual(poll_message_target('https://discord.com/channels/1/2/3', 1, 8), (2, 3))
        self.assertEqual(poll_message_target('3', 1, 8), (8, 3))
        for value in ('https://discord.com/channels/9/2/3', 'https://example.com/channels/1/2/3',
                      '0', '../secret', '999999999999999999999999999'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                poll_message_target(value, 1, 8)

    def test_repository_survives_restart_and_isolates_guilds(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = PollRepository(tmp)
            repository.save(1, 3, {'creator_id': 10})
            repository.save(2, 3, {'creator_id': 20})
            reloaded = PollRepository(tmp)
            self.assertEqual(reloaded.get(1, 3)['creator_id'], 10)
            self.assertEqual(reloaded.get(2, 3)['creator_id'], 20)
            self.assertIsNone(reloaded.get(1, 4))


class PollCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = PollRepository(self.temp.name)
        self.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        self.cog = Polls(self.bot, self.repository)
        self.poll = SimpleNamespace(expires_at=discord.utils.utcnow()+timedelta(hours=24), is_finalised=lambda: False)
        self.message = SimpleNamespace(id=3, jump_url='https://discord.com/channels/1/2/3',
                                       author=SimpleNamespace(id=99), poll=self.poll, end_poll=AsyncMock())
        self.permissions = SimpleNamespace(view_channel=True, read_message_history=True, manage_messages=False)
        self.channel = SimpleNamespace(id=2, send=AsyncMock(return_value=self.message),
            permissions_for=lambda _: self.permissions, fetch_message=AsyncMock(return_value=self.message))
        self.ctx = SimpleNamespace(guild=SimpleNamespace(id=1, get_channel_or_thread=lambda _: self.channel),
            channel=self.channel, author=SimpleNamespace(id=10, guild_permissions=SimpleNamespace(manage_guild=False)),
            send=AsyncMock(), defer=AsyncMock())

    async def test_create_sends_bot_owned_poll_and_persists_creator(self):
        await self.cog.poll.callback(self.cog, self.ctx, 'Dinner?', 'Pizza|Noodles', 2, True)
        sent = self.channel.send.call_args.kwargs['poll']
        self.assertIsInstance(sent, discord.Poll)
        self.assertEqual(sent.question, 'Dinner?')
        self.assertEqual(sent.duration, timedelta(hours=2))
        self.assertTrue(sent.multiple)
        self.assertEqual([answer.text for answer in sent.answers], ['Pizza', 'Noodles'])
        self.assertEqual(self.repository.get(1, 3)['creator_id'], 10)
        self.assertEqual(self.channel.send.call_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_invalid_poll_sends_no_public_message(self):
        await self.cog.poll.callback(self.cog, self.ctx, 'Q', 'A|A')
        self.channel.send.assert_not_awaited()
        self.assertIsNone(self.repository.get(1, 3))

    async def test_creator_can_end_after_repository_reload(self):
        self.repository.save(1, 3, {'creator_id': 10, 'channel_id': 2})
        self.cog.repository = PollRepository(self.temp.name)
        await self.cog.poll_end.callback(self.cog, self.ctx, '3')
        self.message.end_poll.assert_awaited_once()
        self.assertTrue(self.repository.get(1, 3)['ended'])

    async def test_other_member_and_other_guild_are_rejected(self):
        self.repository.save(1, 3, {'creator_id': 20, 'channel_id': 2})
        await self.cog.poll_end.callback(self.cog, self.ctx, '3')
        await self.cog.poll_end.callback(self.cog, self.ctx, 'https://discord.com/channels/4/2/3')
        self.channel.fetch_message.assert_not_awaited()
        self.message.end_poll.assert_not_awaited()

    async def test_admin_can_end_missing_local_record_but_not_other_bots_poll(self):
        self.ctx.author.guild_permissions.manage_guild = True
        self.message.author.id = 98
        await self.cog.poll_end.callback(self.cog, self.ctx, '3')
        self.message.end_poll.assert_not_awaited()
        self.message.author.id = 99
        await self.cog.poll_end.callback(self.cog, self.ctx, '3')
        self.message.end_poll.assert_awaited_once()

    async def test_expired_poll_is_not_ended_again(self):
        self.ctx.author.guild_permissions.manage_guild = True
        self.poll.expires_at = discord.utils.utcnow()-timedelta(seconds=1)
        await self.cog.poll_end.callback(self.cog, self.ctx, '3')
        self.message.end_poll.assert_not_awaited()

    async def test_storage_error_after_send_does_not_duplicate_poll(self):
        self.cog.repository = Mock()
        self.cog.repository.save.side_effect = OSError('disk unavailable')
        with self.assertLogs('bot_app.presentation.discord.polls', level='ERROR'):
            await self.cog.poll.callback(self.cog, self.ctx, 'Q', 'A|B')
        self.channel.send.assert_awaited_once()
        self.assertIn('本地发起人记录保存失败', self.ctx.send.call_args.args[0])
