"""Local UI preview with isolated temporary data; never logs into Discord."""
import asyncio
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aiohttp import web
from bot_app.app.management import ManagementService
from bot_app.app.configuration import JsonSettingsRepository
from bot_app.app.ai import AIRuntime
from bot_app.host.web.server import create_app
from bot_app.shared.identity import Scope, MessageEvent

async def preview():
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    runtime = SimpleNamespace(read_status=lambda: {'state':'offline','music_sessions':{}})
    logs = SimpleNamespace(read=lambda *args,**kwargs: [])
    management = ManagementService(runtime, JsonSettingsRepository(root/'config'), logs)
    management.catalog = lambda: [{'id':'1','name':'Local preview — temporary data',
        'channels':[{'id':'2','name':'bot-testing'},{'id':'3','name':'music-chat'}]}]
    ai = AIRuntime(management, root/'data'); ai.model.key = ''
    management.ai = ai
    message = MessageEvent(Scope('1','2'),'100','10','Preview member','I like jazz.',time.time())
    await ai.memory.call('ingest',message,True)
    await ai.memory.call('queue',message.scope.key,batch_size=1,wait_seconds=10,force=True)
    job = await ai.memory.call('claim')
    await ai.memory.call('apply',job,[{'kind':'fact','subject_id':'10','content':'Likes jazz music.',
        'source_ids':['100'],'slot':'music_preference','attribution':'self_report'}])
    await ai.chat.call('record',message.scope.key,'observed','useful')
    app = create_app(management)
    async def cleanup(_app):
        await ai.close(); temp.cleanup()
    app.on_cleanup.append(cleanup)
    return app

if __name__ == '__main__':
    web.run_app(preview(),host='127.0.0.1',port=8876)
