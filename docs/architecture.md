# Feature-first architecture

The stable application retains v1.3.0 music behavior. Features own their implementation;
`app` composes dependencies, `host` owns platform entry points, and `shared` holds small
infrastructure primitives. This is a single Python process.

```text
main.py                         installed launcher
bot/                            small existing public import aliases
bot_app/
  main.py                       stable module entry point for the supervisor
  app/                          composition, configuration and lifecycle
    bootstrap.py                dependency factories
    main.py                     CLI, --check, process lock and API lifetime
    management.py               administration facade
    configuration.py            atomic per-server JSON settings
    settings_catalog.py         feature schema registry
    settings.py                 runtime application reports
  host/
    discord/bot.py              gateway lifetime and extension registration
    desktop/                    tray, process controller and file IPC
    web/server.py               loopback HTTP API and origin guard
    web/shell/                  administration UI
  features/
    music/                      playback, queue, recovery and personal libraries
      adapters/                 Discord commands and player panels
      storage/                  queue/library JSON and music files
      media/                    extraction and FFmpeg audio
    welcome/                    templates, delivery, deduplication and settings
    polls/                      native polls, validation and creator storage
    utilities/                  ping, sync, emoji and add
    updates/                    release/source verification, staging and rollback
    voice_moderation/           existing stored-only settings
  shared/                       paths, runtime, process locks, logging and contracts
scripts/                        supervisor, setup, tray launchers and release builder
tests/                          grouped by feature and platform
```

The former `application`, `domain`, `infrastructure` and `presentation` trees have been
removed. No duplicate implementation or compatibility layer tree remains. The small
`bot_app.main` and `bot/*` entry points remain for installed launchers and callers.

## Dependencies

- `app` selects concrete implementations and installs factories on the bot.
- `host` and feature adapters receive factories instead of importing composition.
- Cross-feature imports use the destination feature's public `api.py`.
- `shared` does not import features, hosts or composition.
- Small features stay flat; music uses internal subpackages because of its size.
- Architecture tests enforce these boundaries and verify that the updater and supervisor
  import using only the standard library.

## Runtime flows

Discord commands, player buttons and local management controls reach one `PlaybackService`.
Each guild owns one queue, player and voice connection. Mutations run on the bot event loop;
bounded worker threads return metadata and download results. Generation checks reject late
callbacks. Queue snapshots retain the v1.3.0 format and require explicit resume after restart.

Welcome events use screening, deduplication, server settings and permission-checked delivery.
Discord owns poll votes and deadlines; local storage retains the creator for early closing.
The tray and HTTP API share the management facade and acknowledged control messages.

Updates resolve a trusted Release, branch or commit, verify the archive, prepare an isolated
environment and checkpoint queues before a supervised restart. The supervisor journals code
replacement and restores the previous application/environment after a failed check, startup
or readiness timeout. `.env`, configuration, assets, user data and the original environment
remain outside replacement. Old layered release archives and new feature archives are both
accepted; candidate smoke imports select the candidate's actual layout.

See [Releases](releases.md) for pinned source updates and [Music maintenance](music-maintenance.md)
for implementation ownership and operational checks.

## Separate experiments

`codex/haiyang-voice` contains optional local 海洋 wake-word commands, ASR, spoken replies
and v1.4 music extensions such as `playskip`. It retains this architecture and updater.
The stable main branch does not load those features. v1.4.0 is retained as an abandoned
prerelease for historical reference; v1.3.0 is the latest stable Release.

Bot group chat, model clients, memory, persona growth, web browsing and their administration
pages/commands have been removed from both active branches. Existing local data remains on disk.
