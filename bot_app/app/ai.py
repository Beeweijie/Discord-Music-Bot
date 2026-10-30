"""Composition/lifetime of optional chat and memory features."""
from bot_app.features.chat.service import ChatService
from bot_app.features.chat.storage import ChatRepository
from bot_app.features.memory.api import MemoryService
from bot_app.features.memory.storage import MemoryRepository
from bot_app.shared.llm.deepseek import DeepSeekClient
from bot_app.shared.paths import BASE_DIR

class AIRuntime:
    def __init__(self, management, data_dir=None):
        from pathlib import Path
        data_dir = Path(data_dir) if data_dir is not None else BASE_DIR / 'data'
        self.management = management
        self.model = DeepSeekClient(data_dir / 'ai')
        self.chat = None
        self.memory = MemoryService(MemoryRepository(data_dir / 'memory' / 'memory.sqlite3'), self.model,
            lambda guild: management.get_settings('memory', guild), lambda scope: self.chat is not None and self.chat.allowed(scope),
            lambda scope, author: self.chat is not None and author not in self.chat.config(scope)['excluded_users'],
            lambda scope: self.chat.config(scope)['timezone'])
        self.chat = ChatService(ChatRepository(data_dir / 'chat' / 'chat.sqlite3'), self.model,
                                self.memory, lambda guild: management.get_settings('chat', guild))
        self.closed = False

    async def start(self):
        await self.chat.start()
        self.memory.start()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.chat.close()
        await self.memory.close()
        await self.model.close()

    def health(self):
        return {'configured': self.model.configured, 'state': 'ready' if self.model.configured else 'missing_api_key',
                'chat_error': self.chat.last_error, 'memory_error': self.memory.error,
                'semantic_search': self.memory.semantic_state, 'channels': len(self.chat.channels)}
