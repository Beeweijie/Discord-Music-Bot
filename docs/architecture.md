# Backend architecture

`bot_app` is the implementation package. `main.py`, `bot/*`, and
`scripts/tray_app.py` retain the old entry points. Production configuration,
tokens, runtime PIDs and music assets are not migrated or overwritten.

## Responsibilities

- `presentation/discord`: command registration, interactions, panels, welcome
  event adapters and common Discord command errors.
- `presentation/desktop`: the existing Tkinter/pystray frontend.
- `presentation/api`: optional local HTTP adapter.
- `application/music`: event-loop-owned session state, playback, use cases,
  cancellation, concurrency and lifetime management. Operation classes are
  internal parts of one `PlaybackService`, not independently running services.
- `application/welcome`: deduplication, screening and retry policy.
- `application/management`: shared desktop/HTTP management interface.
- `application/updates`: serializes update preparation without blocking the bot loop.
- `application/ports`: storage and transport contracts.
- `domain`: track values, limits, URL rules and control/configuration validation.
- `infrastructure`: media extraction, FFmpeg sources, Discord delivery, process
  ownership, paths and atomic file repositories.
- `bootstrap.py`: dependency composition.
- `scripts/run_bot.py`: a standard-library supervisor that owns restart and rollback.
  `main.py` uses it when executed; importing `main.py` retains the original API.

The domain has no dependency on application, infrastructure or Discord. The
application has no SDK imports or imports of concrete repositories. Music use
cases accept a structural command context (author, guild, channel, send) to
preserve existing responses and channel permissions. A future voice adapter must
supply authenticated member/channel identity, never an identity invented by a
model. Administrator controls intentionally do not require the local desktop
administrator to be in a Discord voice channel, matching the previous UI.

## Concurrency

One bot process owns all music sessions on one asyncio loop. Short user and
administrator mutations share each session's control lock. Queue and playback
locks remain in place. Metadata/download operations run in a bounded four-thread
executor, with at most two concurrent downloads. Metadata workers return new
objects; download workers return paths and never mutate live songs/sessions.
Thread-safe cancellation events and locked downloader caches bridge workers.
Discord's audio callback schedules work back onto the owning loop. Generation
checks reject callbacks from retired sessions. Shutdown signals cancellation,
disposes sessions and waits for owned work before shutting down the executor.
Do not run multiple bot/API workers against the same runtime.

The interprocess tray transport remains atomic files because tray and bot are
separate processes and the tray must manage an offline bot. Commands carry IDs,
timestamps and instance IDs. The bot consumes them on its event loop; desktop
and HTTP adapters both use `ManagementService`. Results retain the last 500 IDs,
while `status.last_control` remains compatible with the tray.

## HTTP interface

The API runs in the bot process and shares its lifetime. Set these environment
variables (or put them in `.env`) before starting `python main.py`:

```dotenv
MANAGEMENT_API_ENABLED=true
MANAGEMENT_API_HOST=127.0.0.1
MANAGEMENT_API_PORT=8766
```

Open `http://127.0.0.1:8766/` directly; no management key or login is required.
Writes require `X-Requested-With: BotDashboard` and a same-origin Origin when supplied. Errors have `error.code` and `error.message`. No CORS wildcard
is enabled. Keep loopback binding with an HTTPS reverse proxy; use HTTPS or a
private VPN for remote access. Disabling HTTP does not disable the desktop UI.

| Method | Path | Result |
|---|---|---|
| GET | `/api/v1/status` | Runtime status and last control result |
| GET | `/api/v1/sessions` | Session snapshots; Discord IDs are strings |
| GET | `/api/v1/settings/voice_moderation` | Existing stored UI settings |
| PUT | `/api/v1/settings/voice_moderation` | Validate and atomically save full settings |
| POST | `/api/v1/sessions/{guild_id}:{channel_id}/controls` | HTTP 202 with command ID |
| GET | `/api/v1/controls/{id}` | Acknowledgement; 404 if pending/expired/unknown |
| GET | `/api/v1/logs?limit=80` | Bounded recent log lines; 1–500 lines |

Control bodies: `{"action":"skip"}`, `{"action":"volume","volume":40}`,
`{"action":"loop","mode":"one"}`. Actions: pause, resume, skip, stop, clear,
volume, loop, seek, replay, shuffle, fair, dedupe, surprise, sleep, remove, nextup,
move. Positional queue controls require the snapshot's `queue_version`.
HTTP 202 means queued, not completed. Poll the result by ID;
`ok` indicates execution success. Commands expire after 60 seconds and cannot
carry over into another bot instance.

Settings body: `{"enabled":false,"level":"normal","model":"small"}`.
Voice moderation was stored-only in the original UI; saving returns
`applied:false, effect:"stored_only"`. This refactor does not invent a moderation
engine. Per-server music settings and durable queues/libraries use atomic JSON
repositories. See `music-maintenance.md` for current behavior, data paths and
the implementation map.

The desktop retains start/stop/restart and Windows startup controls. These are
local OS operations and are not exposed as remote shell endpoints. HTTP stops
with the bot; an independently available administration server would require a
separate supervisor service.

## Verification

Install `requirements.txt`, then run `python -m unittest discover -s tests -v`.
Tests use temporary storage, mocked Discord/media adapters and local HTTP test
servers; they never log into Discord or modify production runtime files.
Real voice playback and upstream YouTube/Bilibili access need a live smoke test
on the deployment machine, including the Pi's media extraction dependencies.

## Per-server settings

Web settings requests use `?guild_id=<Discord server ID>`. Each section is stored atomically in `config/guilds/<guild_id>/<section>.json`. On first access, the current global settings are copied as an independent snapshot, preserving existing limits. Global files remain defaults for new servers and the legacy desktop UI. Music and welcome operations read the selected guild's settings; no shared mutable active-guild state is used.

`default_playlist_url` is editable in the web dashboard and with `!playlist_defult <URL>` (Manage Server permission required). Empty /play input uses this server's saved URL. Existing queue and playback persistence are unchanged. Shared cache files honor the longest retention among their recorded guild owners while in-use files remain protected.
