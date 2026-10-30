"""Optional message bursts use one generation, bounded parts and durable delivery guards."""
import copy
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bot_app.features.chat.delivery import reply_parts
from bot_app.features.chat.settings import DEFAULTS
from bot_app.features.chat.service import ChatService,ChannelState
from bot_app.features.chat.storage import ChatRepository
from bot_app.features.memory.api import MemoryService
from bot_app.features.memory.storage import MemoryRepository
from bot_app.features.memory.settings import DEFAULTS as MEMORY_DEFAULTS
from bot_app.shared.identity import Scope,MessageEvent


def event(identifier,text,bot=False):
    return MessageEvent(Scope('1','2'),identifier,'99' if bot else '3','bot' if bot else 'member',text,time.time(),
                        is_bot=bot,direct=not bot,bot_user_id='99')


class BurstTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg=dict(copy.deepcopy(DEFAULTS),enabled=True,channels=['2'],mode='participate')
        self.model=SimpleNamespace(complete=AsyncMock(return_value='这点挺有意思。<message_break>我刚才也想到这个了。'))
        self.memory=MemoryService(MemoryRepository(Path(self.tmp.name)/'memory.db'),self.model,
            lambda _:dict(MEMORY_DEFAULTS,enabled=True),lambda _:True)
        self.chat=ChatService(ChatRepository(Path(self.tmp.name)/'chat.db'),self.model,self.memory,lambda _:self.cfg)
        self.chat.pause=AsyncMock()
        self.addAsyncCleanup(self.memory.close); self.addAsyncCleanup(self.chat.close)
        self.state=ChannelState(messages=deque([event('100','你怎么看？')]),sequence=1)
        self.state.can_send=AsyncMock(return_value=True)
        self.state.send=AsyncMock(side_effect=[event('200','这点挺有意思。',True),event('201','我刚才也想到这个了。',True)])

    async def test_natural_two_part_reply_is_generated_once(self):
        await self.chat.round('discord:1:2',self.state,1)
        self.model.complete.assert_awaited_once()
        self.assertEqual(self.model.complete.await_args.kwargs['max_tokens'],300)
        self.assertEqual(self.state.send.await_count,2)
        self.chat.pause.assert_awaited_once()
        self.assertNotIn('<message_break>',str(self.state.send.await_args_list))

    async def test_new_input_during_pause_stops_the_rest(self):
        async def pause(_seconds):
            self.state.sequence+=1
        self.chat.pause.side_effect=pause
        await self.chat.round('discord:1:2',self.state,1)
        self.assertEqual(self.state.send.await_count,1)
        rows=await self.memory.call('recent','discord:1:2')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['content'],'这点挺有意思。')

    async def test_partial_unknown_send_is_never_blindly_retried(self):
        self.state.send.side_effect=[event('200','这点挺有意思。',True),TimeoutError()]
        with self.assertRaises(TimeoutError):
            await self.chat.round('discord:1:2',self.state,1)
        await self.chat.round('discord:1:2',self.state,1)
        self.assertEqual(self.state.send.await_count,2)

    def test_short_messages_are_not_forced_into_bursts_and_parts_are_capped(self):
        self.assertEqual(reply_parts('嗯哼。',self.cfg),['嗯哼。'])
        self.assertEqual(len(reply_parts('a<message_break>b<message_break>c<message_break>d',self.cfg)),3)
        self.assertEqual(reply_parts('a<message_break>b',dict(self.cfg,reply_burst_enabled=False)),['a\nb'])
