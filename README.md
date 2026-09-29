# Discord Music Bot

## Quick Windows setup

1. Download `Discord-Music-Bot-*-windows.zip` from [GitHub Releases](https://github.com/Beeweijie/Discord-Music-Bot/releases/latest) and **extract the entire archive** to a permanent folder. Do not run the installer inside the ZIP.
2. Double-click **`install.bat`** in the root folder. It detects or installs Python 3.13 and FFmpeg, creates an isolated environment, and installs dependencies. Initial setup requires internet access. If winget is unavailable, install **App Installer** from Microsoft Store, or install Python 3.13 and FFmpeg manually.
3. On first setup, Notepad opens `.env`. Replace `DISCORD_TOKEN=your_token_here` with your bot token, save the file, and close Notepad. Setup checks the configuration without connecting to Discord.
4. Double-click **`start.bat`** to launch the tray app. Use its menu to start the bot, open the web dashboard, or enable startup at Windows login. The dashboard defaults to `http://127.0.0.1:8766/`.

Create an application in the [Discord Developer Portal](https://discord.com/developers/applications), get its token from the **Bot** page, and enable **Server Members Intent** and **Message Content Intent**. Invite it with the `bot` and `applications.commands` scopes and grant View Channels, Send Messages, Embed Links, Connect, and Speak permissions. Polls also require Send Polls. Keep the token in your local `.env`; do not upload or share it.

For a manual upgrade, exit the old tray app and back up `.env`, `config/`, `data/`, and any local music in `assets/music/`. Extract the new release into a new folder, copy those files into it, and run `install.bat`. Re-enable startup from the new tray app if needed. Setup preserves an existing `.env`. Release packages exclude credentials, server data, logs, and caches.

Advanced options: `install.bat -NoConfigure` installs dependencies so you can configure `.env` later; `-InstallStartup` creates a Windows login shortcut. Source checkouts use the same root launchers. The Windows installer supports Python 3.12–3.14.

## Update from Discord

The bot owner can run **`!update`** to install the latest stable GitHub Release and restart automatically. You can also use `!update https://github.com/Beeweijie/Discord-Music-Bot`. The URL must match the trusted repository configured with `UPDATE_GITHUB_URL` in `.env`; the default is this repository. Server administrators who do not own the bot cannot update it.

The bot stays online while it downloads the ZIP, verifies its SHA-256 checksum and manifest, and installs dependencies into a separate environment. It then saves queues, briefly disconnects, replaces application files, and restarts. `.env`, all existing `config/` files, music assets, user data, logs, and the original environment are preserved. Use `/resume` after restarting to continue a saved queue. New code and its environment are restored to the previous version if configuration checks or Discord startup fail.

Run through `start.bat`, the tray, or `python main.py` so the restart supervisor is active. Install v1.1.1 or newer once using the manual instructions above; after that, use `!update`. Versions before v1.1.0 lack the command, and v1.1.0 needs the v1.1.1 download-header fix. Public releases must contain both the Windows ZIP and SHA-256 assets. Private repositories, prereleases and downgrades are not supported. If an update fails, check `logs/update.log` and `runtime/updates/<job-id>/`. Backups are kept there for manual recovery; keep this folder while an update is in progress. Restart the normal launcher to recover an interrupted installation.

Join a voice channel and use `/play` with a song name or URL. Leave the input empty to use your server's default playlist, or resume a saved queue. Use **Add song**, **Pause / Resume**, **Skip**, and **Favorite** on the Discord player. The player displays the original cover image, without a progress bar.

The local dashboard is available at `http://127.0.0.1:8766/`. Controls, command descriptions and operation messages are in English. Welcome messages may remain in Chinese.

### New music tools

- **Queue:** `/queue page:2` or the Previous / Next buttons. The web dashboard supports search, pages, Play next, Remove, Shuffle, Fair queue and Remove duplicates. Stale web queue positions are rejected instead of changing the wrong track.
- **Your library:** `/favorite`, `/favorites`, `/unfavorite index:1`. `/playlist_save name:Chill` saves the current track plus queue; `/playlist_load name:Chill` appends it. `/playlist_load name:favorites` plays favorites. `/playlists` lists saved playlists; `/playlist_delete` removes one. Names autocomplete in Discord. Personal libraries are isolated by server and user and survive restarts.
- **Fair queue:** `/fair` takes turns by requester while preserving each person's order. `/dedupe` removes duplicate waiting tracks. `/nextup index:3` moves track 3 to the front. `/surprise` picks a random next track without interrupting the current song.
- **Sleep:** `/sleep minutes:30` pauses after 30 minutes and keeps the queue. `/sleep minutes:0` cancels the timer. Timers are checked every 15 seconds and persist across restarts.
- **Paused for 10 minutes:** disconnect automatically and save the current position and queue. The check runs every 30 seconds. `/resume` reconnects and continues; the dashboard can resume when someone is in the original voice channel. Empty-channel timeout can disconnect earlier.
- **History:** `/history page:1` shows the latest 100 completed/skipped tracks in this session. The web dashboard shows the latest 20. History survives restart and automatic disconnection; explicit `/stop` clears the session.
- **Playback fixes:** volume changes apply immediately. Skip bypasses track repeat. `/replay` restarts without adding queue duplicates. `/seek +30` and `/seek -15` move relative to the current position.

See [the maintenance guide](docs/music-maintenance.md) for code ownership, storage, testing and common changes.

## Discord commands

`both` supports `/command` and `!command`; `!` supports prefix commands only; `/` supports slash commands only.

| Command | Support | Parameters | Description |
|---|---|---|---|
| `replay` | both | None | Restart the current song |
| `fair` | both | None | Alternate requesters while preserving each user's order |
| `dedupe` | both | None | Remove duplicate waiting tracks |
| `nextup` | both | `index: int` | Move the selected track to the front of the queue |
| `surprise` | both | None | Choose a random next track without interrupting playback |
| `sleep` | both | `minutes: int = 0` | Schedule a pause and preserve the queue; 0 cancels |
| `history` | both | `page: int = 1` | Show recently played tracks in this voice channel |
| `favorite` | both | None | Favorite the current song |
| `favorites` | both | `page: int = 1` | Show your favorites in this server |
| `unfavorite` | both | `index: int` | Remove a favorite by its index |
| `playlist_save` | both | `name: str` | Save the current track and queue as a personal playlist |
| `playlist_load` | both | `name: str` | Append a personal playlist; favorites loads your favorites |
| `playlists` | both | None | List your personal playlists |
| `playlist_delete` | both | `name: str` | Delete a personal playlist without interrupting playback |
| `join` | both | None | Join your voice channel |
| `playlist_defult` | ! | `url` | Set this server's default playlist; requires Manage Server. Example: `!playlist_defult https://music.youtube.com/playlist?list=PLH6zD0MCw2r4` |
| `seek` | both | `position` | Seek to a timestamp, such as `/seek 1:30` or `!seek 90`; 0 restarts |
| `poll` | both | `question options hours multiple` | Create a poll with pipe-separated options; defaults to 24 hours and single choice |
| `poll_end` | both | `message` | End a poll early using a message link or an ID from this channel; creator or administrator only |
| `play` | both | `input` | Play local MP3s, YouTube/Bilibili links, playlists or collections, or search keywords; `/play` supports autocomplete |
| `queue` | both | None | Show this voice channel's queue |
| `pause` | both | None | Pause playback |
| `resume` | both | None | Resume playback |
| `now` | both | None | Show the current track and player controls |
| `remove` | both | `index` | Remove a queued track by its 1-based index |
| `volume` | both | `volume` | Set this session's volume from 0 to 100 |
| `shuffle` | both | None | Shuffle waiting tracks |
| `skip` | both | None | Skip the current track, or try starting the next track when idle |
| `stop` | both | None | Stop playback, clear the queue and disconnect |
| `clear` | both | None | Clear waiting tracks while preserving the current song |
| `move` | both | `from_index`, `to_index` | Reorder waiting tracks using 1-based indices |
| `loop` | both | `mode` | Set repeat mode: off, one track, or the queue |
| `help_music` | both | None | Show music command help |
| `ping` | both | None | Show the bot's Discord latency |
| `sync` | ! | None | Sync global slash commands; bot owner only |
| `update` | ! | Optional GitHub repository URL | Install the latest stable release and restart; bot owner only |
| `a` | ! | None | Alias for `!sync`; bot owner only |
| `emoji` | ! | None | Show configured emoji |
| `add` | ! | `a`, `b` | Add two integers |
| `help` | ! | Optional command name | List commands or show help for one command |

There are currently no slash-only commands. Player buttons use the same operations as commands.

### Playback recovery and Discord player

- HTTP 403 download failures trigger a fresh page lookup and up to two additional attempts, with another audio format on the final attempt. Persistent failures skip the track.
- The queue, current track, volume, repeat mode and position are stored in `data/queues/<guild-id>/<voice-channel-id>.json`. Changes are saved immediately; playback position is checkpointed every 15 seconds. Restart does not automatically join voice. Use `/resume` or an empty `/play` in the original channel to continue. A new queue's empty `/play` uses the server's default playlist.
- Explicit stop/clear removes the relevant queue state. An abrupt interruption may lose approximately 15 seconds of playback progress.
- One Discord message updates the current track, cover, next track and controls. Temporary network failures preserve the message and retry; deleted messages are recreated. The cover changes with each track. The Discord player does not display playback progress.

### Polls

Use `/poll` with a question and 2–10 options separated by `|`. `hours` accepts 1–768 and defaults to 24. Set `multiple` to true to allow multiple choices. Prefix example: `!poll "What is for dinner?" "Hot pot|Pizza|Noodles" 24 false`.

Use `/poll_end message:MESSAGE_LINK` or `!poll_end MESSAGE_LINK` to end a poll early. Only its creator or a member with Manage Server or Manage Messages in the channel can do this.

Discord native polls preserve votes and deadlines even while the bot is offline, and expire automatically. Creator and management records are stored per server in `data/polls/`. Polls are not anonymous; members can inspect votes through Discord. Both the bot and the creator need permission to send polls.
