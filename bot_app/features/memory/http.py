"""Scoped local-administrator memory inspection and correction routes."""
from pathlib import Path
from aiohttp import web
from bot_app.shared.identity import snowflake

def register(app, management, guild_scope):
    async def memories(request):
        scope = management.memory_scope(guild_scope(request), request.query.get('channel_id', ''))
        subject = request.query.get('subject_id') or None
        if subject:
            subject = snowflake(subject)
        try:
            limit, offset = int(request.query.get('limit', 50)), int(request.query.get('offset', 0))
        except ValueError:
            raise ValueError('Pagination must be numeric.') from None
        if not 1 <= limit <= 1000 or not 0 <= offset <= 10000000:
            raise ValueError('Invalid pagination range.')
        return web.json_response({'memories': await management.ai.memory.list(scope, subject, candidates=True, limit=limit, offset=offset, history=request.query.get('history') == '1')})
    async def change(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('Expected an object.')
        scope = management.memory_scope(body.get('guild_id'), body.get('channel_id'))
        memory_id = request.match_info['memory_id']
        if request.method == 'DELETE':
            result = await management.ai.memory.forget(scope, memory_id=memory_id)
        elif request.match_info.get('operation') == 'correct':
            result = await management.ai.memory.correct(scope, memory_id, body.get('text'))
        elif request.match_info.get('operation') == 'approve':
            result = await management.ai.memory.approve(scope, memory_id)
        else:
            raise ValueError('Unsupported memory operation.')
        await management.ai.chat.clear_context(scope)
        return web.json_response({'result': result})
    async def forget(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('Expected an object.')
        scope = management.memory_scope(body.get('guild_id'), body.get('channel_id'))
        count = await management.ai.memory.forget(scope, snowflake(body.get('subject_id')))
        await management.ai.chat.clear_context(scope)
        return web.json_response({'forgotten': count})
    async def script(request):
        return web.FileResponse(Path(__file__).with_name('web') / 'page.js')
    app.add_routes([web.get('/memory.js', script), web.get('/api/v1/memory', memories),
        web.delete('/api/v1/memory/{memory_id}', change),
        web.post('/api/v1/memory/{memory_id}/{operation}', change),
        web.post('/api/v1/memory/forget', forget)])
