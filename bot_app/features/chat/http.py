"""Chat dashboard transport, mounted by the web host."""
from pathlib import Path
from aiohttp import web

def register(app, management, guild_scope):
    async def status(request):
        return web.json_response(await management.ai_status(guild_scope(request)))
    async def decisions(request):
        return web.json_response({'decisions': await management.chat_decisions(guild_scope(request))})
    async def script(request):
        return web.FileResponse(Path(__file__).with_name('web') / 'page.js')
    async def style(request):
        return web.FileResponse(Path(__file__).with_name('web') / 'style.css')
    app.add_routes([web.get('/ai.js', script), web.get('/ai.css', style),
        web.get('/api/v1/ai/status', status), web.get('/api/v1/chat/decisions', decisions)])
