"""Composition/lifetime of optional chat and memory features."""
from bot_app.features.chat.service import ChatService
from bot_app.features.chat.storage import ChatRepository
from bot_app.features.memory.api import MemoryService
from bot_app.features.memory.storage import MemoryRepository
from bot_app.features.web_search.api import WebSearchService
from bot_app.shared.llm.deepseek import DeepSeekClient
from bot_app.shared.paths import BASE_DIR
from bot_app.features.persona.api import PersonaService
from bot_app.features.persona.storage import PersonaRepository
from bot_app.features.vision.api import VisionService

class AIRuntime:
    def __init__(self, management, data_dir=None):
        from pathlib import Path
        data_dir = Path(data_dir) if data_dir is not None else BASE_DIR / 'data'
        self.management = management
        self.model = DeepSeekClient(data_dir / 'ai')
        self.web = WebSearchService(self.model)
        self.chat = None
        self.vision = VisionService(self.model)
        self.persona = PersonaService(PersonaRepository(data_dir/'ai'/'persona.sqlite3'),self.model,
            lambda scope:self.chat.config(scope),lambda scope:self.chat is not None and self.chat.allowed(scope))
        self.memory = MemoryService(MemoryRepository(data_dir / 'memory' / 'memory.sqlite3'), self.model,
            lambda guild: management.get_settings('memory', guild), lambda scope: self.chat is not None and self.chat.allowed(scope),
            lambda scope, author: self.chat is not None and author not in self.chat.config(scope)['excluded_users'],
            lambda scope: self.chat.config(scope)['timezone'])
        self.chat = ChatService(ChatRepository(data_dir / 'chat' / 'chat.sqlite3'), self.model,
                                self.memory, lambda guild: management.get_settings('chat', guild),web=self.web,
                                persona=self.persona,vision=self.vision)
        self.memory.on_forget = self.persona.forget
        self.closed = False

    async def start(self):
        await self.chat.start()
        self.memory.start()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.chat.close()
        await self.persona.close()
        await self.memory.close()
        await self.model.close()

    def health(self):
        return {'configured': self.model.configured, 'state': 'ready' if self.model.configured else 'missing_api_key',
                'chat_error': self.chat.last_error, 'memory_error': self.memory.error,
                'semantic_search': self.memory.semantic_state, 'web_search':self.web.state, 'vision':self.vision.state,
                'persona_error':self.persona.error,'channels': len(self.chat.channels)}
