"""Versioned, local management API (optional, loopback by default)."""
import logging
from pathlib import Path
from aiohttp import web

logger = logging.getLogger(__name__)


def _wire(value):
    """Discord snowflakes are strings on the browser-facing wire."""
    if isinstance(value, dict):
        return {k: str(v) if k in {"guild_id", "channel_id", "requester_id"} and v is not None
                else _wire(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_wire(v) for v in value]
    return value


def create_app(management):
    static_dir = Path(__file__).resolve().parent / "web"

    @web.middleware
    async def boundary(request, handler):
        try:
            if request.method not in {"GET", "HEAD"}:
                if request.headers.get("X-Requested-With") != "BotDashboard":
                    raise web.HTTPForbidden(reason="Invalid request origin.")
                origin = request.headers.get("Origin")
                if origin and origin != f"{request.scheme}://{request.host}":
                    raise web.HTTPForbidden(reason="Invalid request origin.")
            response = await handler(request)
        except ValueError as error:
            response = web.json_response({"error": {"code": "invalid_request", "message": str(error)}}, status=400)
        except web.HTTPException as error:
            response = web.json_response({"error": {"code": "http_error", "message": error.reason}}, status=error.status)
        except Exception:
            logger.exception("Management request failed")
            response = web.json_response({"error": {"code": "internal_error", "message": "Operation failed. Check the local log."}}, status=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    app = web.Application(middlewares=[boundary], client_max_size=16 * 1024)

    async def asset(request):
        filename = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[request.path]
        return web.FileResponse(static_dir / filename)

    def guild_scope(request):
        guild_id = request.query.get("guild_id")
        if guild_id is not None and guild_id not in {g["id"] for g in management.get_guilds()}:
            raise ValueError("Select a server the bot has joined.")
        return guild_id

    async def all_settings(request):
        return web.json_response(management.all_settings(guild_scope(request)))

    async def guilds(request):
        return web.json_response(management.get_guilds())

    async def status(request):
        return web.json_response(_wire(management.get_status()))

    async def music_sessions(request):
        return web.json_response(_wire(management.get_sessions()))

    async def settings(request):
        section = request.match_info["section"]
        guild_id = guild_scope(request)
        if request.method == "GET":
            return web.json_response(management.get_settings(section, guild_id))
        management.update_settings(section, await request.json(), guild_id)
        return web.json_response(dict(management.settings_report(section, guild_id), saved=True))

    async def control(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be an object.")
        body["session_id"] = request.match_info["session_id"]
        command_id = management.submit_control(body)
        return web.json_response({"id": command_id, "state": "accepted"}, status=202)

    async def result(request):
        value = management.get_control_result(request.match_info["command_id"])
        if value is None:
            raise web.HTTPNotFound(reason="Command result is pending, expired or unknown")
        return web.json_response(value)

    async def logs(request):
        details = request.query.get("details", "false")
        if details not in {"true", "false"}:
            raise ValueError("Invalid detail filter")
        return web.json_response({"lines": management.get_logs(int(request.query.get("limit", "80")),
            guild_id=guild_scope(request), level=request.query.get("level", "INFO"), details=details == "true")})

    app.add_routes([
        web.get("/", asset), web.get("/app.js", asset), web.get("/style.css", asset),
        web.get("/api/v1/settings", all_settings), web.get("/api/v1/guilds", guilds),
        web.get("/api/v1/status", status),
        web.get("/api/v1/sessions", music_sessions),
        web.get("/api/v1/settings/{section}", settings),
        web.put("/api/v1/settings/{section}", settings),
        web.post("/api/v1/sessions/{session_id}/controls", control),
        web.get("/api/v1/controls/{command_id}", result),
        web.get("/api/v1/logs", logs),
    ])
    return app


async def start_server(management, host="127.0.0.1", port=8766):
    runner = web.AppRunner(create_app(management), access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
