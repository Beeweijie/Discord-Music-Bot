# Music maintenance guide

## Run and verify

Use Python from the project environment, install `requirements.txt`, and run:

```powershell
.\.venv\Scripts\python.exe main.py --check
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe main.py
```

`--check` checks configuration and FFmpeg without connecting to Discord. Never
run a second bot against the same data directory. System events are in `logs/system.log`; server activity is in `logs/guilds/<guild-id>/activity.log`.
Do not print `.env` or include it in support reports.

## Where to make a change

| Change | File |
|---|---|
| Register a Discord command or autocomplete | `bot_app/presentation/discord/music.py` |
| Buttons, Add song modal, paginated queue, cover embed | `bot_app/presentation/discord/panels.py` |
| Shared command dispatch and lifetime | `bot_app/application/music/service.py` |
| Basic play/pause/stop/seek commands | `bot_app/application/music/commands.py` |
| Queue editing, personal library and sleep timer | `bot_app/application/music/extras.py` |
| Voice connection, playback, callback and downloads | `bot_app/application/music/playback.py` |
| Session retirement, idle checks and web control execution | `bot_app/application/music/sessions.py` |
| Queue snapshots, restoration and checkpoints | `bot_app/application/music/recovery.py` |
| Track identity and fair queue algorithm | `bot_app/domain/music/library.py` |
| Web control payload validation | `bot_app/domain/control.py` |
| Per-server defaults and limits | `bot_app/domain/settings.py` |
| Atomic personal library storage | `bot_app/infrastructure/persistence/library.py` |
| Plain web UI | `bot_app/presentation/api/web/` |

The operation classes are parts of **one** PlaybackService, not separate services.
Dependencies are injected in `bootstrap.py`. Older media/presenter forwarding
methods remain for the compatibility facade in `bot/music.py`; avoid adding more
layers for a small feature. Use explicit imports, not wildcard imports.

## Invariants to preserve

1. All live session changes happen on the bot's asyncio loop. SDK audio callbacks
   return to that loop; workers only extract metadata/download files.
2. Short Discord and web controls share `control_lock`. Queue mutations also
   acquire `queue_lock`. Never hold the control lock while downloading media.
3. Retirement invalidates generation/playback callbacks before stopping audio.
   Automatic retirement clones portable metadata before cancelling old tasks.
   Explicit Stop clears the queue; idle or 10-minute pause retirement saves it.
4. Queue indices are 1-based. Web remove/move/nextup must include the 16-character
   `queue_version` from the session snapshot. Reject stale versions before editing.
5. Keep favorites per guild **and** user. Saved tracks never contain cache paths,
   asyncio tasks, voice objects or authentication data. Playback is newly resolved.
6. Skip sets `suppress_repeat`; Replay uses seek-to-zero. Neither inserts an
   accidental duplicate in single-track repeat mode.
7. Panel failures must not interrupt music. Preserve the message ID on transient
   failures; recreate only if Discord says the message no longer exists.
8. UI text is English. Track titles, member names and conversational welcome
   messages can be Chinese. Keep the Discord cover static and omit progress.

## Data and backup

- `config/guilds/<guild>/music.json`: independent server settings, initialized
  from global defaults. Existing settings are not overwritten by a code upgrade.
- `data/queues/<guild>/<voice>.json`: current/preparing track, queue, position,
  volume, loop, recent history, sleep deadline and player message identity.
- `data/library/<guild>/<user>.json`: up to 500 favorites and 20 named personal
  playlists (up to the configured queue plus one current track per playlist).
- `assets/music/cache/`: disposable audio cache, never the source of durable state.

Back up `config/guilds`, `data/queues` and `data/library`. Atomic JSON writes keep
the previous snapshot intact if a write fails. Corrupt files are retained and
reported; do not silently replace a broken library with an empty one.

Restart does not autojoin a voice channel. Resume explicitly with `/resume`,
empty `/play`, or the dashboard. Checkpoints occur every 15 seconds; an abrupt
kill can lose the latest few seconds. The 10-minute paused timeout is checked
every 30 seconds. Timers use a wall-clock deadline so they survive restart;
playback position and continuous pause duration use monotonic time.

History is bounded session history, not a permanent listening analytics service.
Library snapshots save song order, not playback offsets. Playlist loading appends
up to the queue limit and reports the accepted count; it never clears the queue.

## Verification before release

Automated tests cover guild/user isolation, damaged storage, stale web controls,
fair queues, canonical YouTube deduplication, timer expiry, pause retirement,
restoration, old audio callbacks, live volume, native poll registration, media
retries and API validation. No live account or production queue is used in tests.

For a live smoke test, use a test voice channel: play a local MP3, adjust volume,
pause, seek, resume, then try one supported remote track. Confirm cover updates
and buttons in Discord. A mocked suite cannot guarantee upstream YouTube/Bilibili
availability, audio audibility, or the permissions in every Discord channel.

## Logging

The dashboard Logs page selects one server or System. Activity is the default:
short single-line messages without terminal colors or tracebacks. Use Minimum level
to focus on warnings/errors. Enable Diagnostic details and select All levels for
downloader messages and full exception traces.

Detailed files are `logs/system.debug.log` and `logs/guilds/<guild-id>/debug.log`.
Each file rotates at 2 MiB with three backups. At most 32 scopes stay open.
Identical activity messages within 60 seconds are suppressed; the next matching
message after that interval reports the repeat count. Details retain every message.
Discord heartbeats and routine HTTP internals are excluded. Task-local identity is
propagated into media worker threads; server records are never copied into System.
The old `logs/bot.log` remains historical output and a fallback for uncaught stderr;
its mixed historical entries are not retroactively assigned to servers.
