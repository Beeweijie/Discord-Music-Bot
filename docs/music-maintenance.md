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
| Register a Discord command or autocomplete | `bot_app/features/music/adapters/discord.py` |
| Buttons, Add song modal, paginated queue, cover embed | `bot_app/features/music/adapters/panels.py` |
| Shared command dispatch and lifetime | `bot_app/features/music/service.py` |
| Basic play/pause/stop/seek commands | `bot_app/features/music/commands.py` |
| Queue editing, personal library and sleep timer | `bot_app/features/music/extras.py` |
| Voice connection, playback, callback and downloads | `bot_app/features/music/playback.py` |
| Session retirement, idle checks and web control execution | `bot_app/features/music/sessions.py` |
| Queue snapshots, restoration and checkpoints | `bot_app/features/music/recovery.py` |
| Track identity and fair queue algorithm | `bot_app/features/music/library_rules.py` |
| Web control payload validation | `bot_app/features/music/control.py` |
| Per-server defaults and limits | `bot_app/features/music/settings.py` |
| Atomic personal library storage | `bot_app/features/music/storage/library.py` |
| Plain web UI | `bot_app/host/web/shell/` |

The operation classes are parts of **one** PlaybackService, not separate services.
Dependencies are injected in `bot_app/app/bootstrap.py`. Older media/presenter forwarding
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
- `data/queues/<guild>/queue.json`: the server's current/preparing track, queue, position,
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

One bot account serves multiple guilds. Each guild has one player, one queue and
at most one voice connection. The server ID is the stable session/control key;
the voice channel is only its current or last location. `/join` and administrator
moves keep the current track, queue, position, volume, repeat mode and panel.
Other playback commands require the member to share the bot's voice channel.
When disconnected, `/resume` can continue the server queue in any voice channel.
Command moves and voice events share the guild connection lock. Voice recovery
blocks new connection attempts throughout that guild until recovery finishes.

Legacy `<guild>/<voice>.json` queues merge on first restore: the latest snapshot
supplies settings, location and the first tracks; other channel tracks append
with duplicates removed. Original files remain as backups. A server snapshot
takes precedence thereafter, including an empty snapshot after Stop, so old
queues cannot reappear. Migration retains existing tracks even if their combined
count exceeds the current enqueue limit; new additions still respect that limit.
If an update rolls back and the older bot writes newer channel snapshots, the
next upgrade remigrates those snapshots instead of using the earlier server copy.

A voice leave event can be part of discord.py's reconnect flow. Allow the SDK
60 seconds to recover before retiring the session, freeze its playback position,
and retain the current track if the old audio callback arrives during recovery.
The application does not force-rejoin a kicked bot. If recovery fails, save the
queue for explicit `/resume`; Stop and shutdown cancel the pending recovery.
Unchanged player panels are not edited on every checkpoint; explicit `/now`
still checks the message so a deleted panel can be recreated.

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
Discord lifecycle INFO messages record voice handshakes and recovery outcomes;
payload-bearing DEBUG messages and routine heartbeats remain excluded. Gateway
disconnect/resume events include downtime. Session retirement records its reason,
queue preservation, current track, position and human count. Detailed entries
include guild/channel identity as well as full exception traces. Task-local identity is
propagated into media worker threads; server records are never copied into System.
The old `logs/bot.log` remains historical output and a fallback for uncaught stderr;
its mixed historical entries are not retroactively assigned to servers.

Music background tasks (checkpoints, controls, idle checks, preloads and audio
callbacks) are tracked with task names. Unexpected task failures are logged
immediately with their traceback and inherited guild/channel identity. Normal
cancellation is not reported as a bug. A failed per-session checkpoint is logged
and retried on the next interval without stopping checks for other servers.
Search, playlist, voice connection, audio and dashboard failures retain their
original exception traces in the detailed files, even when Discord receives a
short error reply. Handled download failures are not duplicated as task errors.
An event-loop exception handler catches unowned async task/callback failures.

System logs identify process startup by PID and instance ID, and record requested
shutdown and orderly exit. The supervisor records child exit codes in
`logs/update.log`, including a child that crashes before bot logging starts.
A power loss or forced kill cannot guarantee a final message from the bot;
use the supervisor and host OS logs to investigate those cases. Log rotation and
the dashboard's bounded recent-line view are not a permanent incident archive.
Logic errors that raise no exception still require state checks, reproduction
and regression tests; logging alone cannot detect every bug.
