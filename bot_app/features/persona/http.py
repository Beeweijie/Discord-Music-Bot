"""Local administrator inspection and revision rollback."""
from pathlib import Path
from aiohttp import web


def register(app, management, guild_scope):
    async def state(request):
        scope = management.memory_scope(guild_scope(request),request.query.get('channel_id',''))
        return web.json_response(await management.ai.persona.status(scope))

    async def revert(request):
        body = await request.json()
        if not isinstance(body,dict) or type(body.get('revision_id')) is not int:
            raise ValueError('Select a persona revision to revert.')
        scope = management.memory_scope(body.get('guild_id'),body.get('channel_id'))
        await management.ai.persona.revert(scope,body['revision_id'])
        await management.ai.chat.clear_context(scope)
        return web.json_response({'reverted':True})

    async def script(request):
        return web.FileResponse(Path(__file__).with_name('web')/'page.js')

    app.add_routes([web.get('/persona.js',script),web.get('/api/v1/persona',state),
                    web.post('/api/v1/persona/revert',revert)])
