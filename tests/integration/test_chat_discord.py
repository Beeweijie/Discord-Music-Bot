"""Transport identity checks; no login, model calls, or Discord messages."""
import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from bot_app.features.chat.discord import Chat
from bot_app.features.chat.settings import DEFAULTS


class RecipientIdentityTests(unittest.TestCase):
    def setUp(self):
        settings=copy.deepcopy(DEFAULTS)
        settings.update(name='蓝色大肥鱼',aliases=['蓝色大肥鱼'])
        management=SimpleNamespace(get_settings=Mock(return_value=settings))
        self.cog=Chat(SimpleNamespace(user=SimpleNamespace(id=99),management=management),None)

    def message(self,text,mentions=(),reply_author=None):
        reference=None if reply_author is None else SimpleNamespace(message_id=50,
            resolved=SimpleNamespace(author=SimpleNamespace(id=reply_author)))
        return SimpleNamespace(id=100,guild=SimpleNamespace(id=1),channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=3,display_name='群友',bot=False),content=text,
            created_at=datetime.now(timezone.utc),mentions=list(mentions),reference=reference)

    def test_other_bot_with_matching_display_name_is_not_self(self):
        other=SimpleNamespace(id=88,display_name='蓝色大肥鱼')
        event=self.cog.event(self.message('<@88>',[other]))
        self.assertFalse(event.direct)
        self.assertTrue(event.mention_only)
        self.assertEqual(event.bot_user_id,'99')

    def test_self_mention_matches_id_even_if_display_name_differs(self):
        me=SimpleNamespace(id=99,display_name='喵酱')
        event=self.cog.event(self.message('<@99>',[me]))
        self.assertTrue(event.direct)
        self.assertEqual(event.mentioned_user_ids,['99'])

    def test_reply_target_is_checked_by_author_id(self):
        self.assertFalse(self.cog.event(self.message('你好',reply_author=88)).direct)
        self.assertTrue(self.cog.event(self.message('你好',reply_author=99)).direct)

    def test_configured_plain_text_alias_still_addresses_self(self):
        self.assertTrue(self.cog.event(self.message('蓝色大肥鱼，你怎么看？')).direct)
