# Discord Music Bot

The implementation is organized by feature under `bot_app/features/`, with platform hosts in
`bot_app/host/` and dependency wiring in `bot_app/app/`. See the [architecture](docs/architecture.md).

## DeepSeek group chat and memory (optional)

The bot can observe a channel or decide when to join its conversation. Set `DEEPSEEK_API_KEY`
in your local `.env`, enable **Message Content Intent** in Discord's developer portal, and use
the dashboard's **Group chat** page to enable a server and explicitly select test channels.
Start in **Observe** mode, review decisions, then switch a channel to **Participate**.
Manage Server members can also use `/chat observe`, `/chat on`, `/chat off` and `/chat status`.

Memory has a separate opt-in switch on the **Memory** page. It stores scoped facts, events,
summaries and evidence in local SQLite, supports review/correction/forgetting, and works with
keyword recall without an embedding service. `/memory` manages your own channel profile.
Both features start disabled. Chat does not execute music controls or arbitrary commands.
Read-only web search can be enabled separately with **Allow the bot to search the web when needed**.
The model then chooses whether to call DeepSeek's official Search/Fetch APIs using the same API key,
and cites retrieved sources. Casual chat does not require a lookup. Tool calls have per-reply caps.
Choose **Active** participation for quicker, unmentioned follow-ups and more spontaneous replies.
Its timing policy adapts to questions, ongoing exchanges and channel activity; it never needs an @
to consider joining the discussion. Commands and mentions addressed to other bots remain protected.

Image understanding and gradual persona development have separate opt-in switches. Discord image
attachments are described once for context; raw images and signed attachment URLs are not retained.
The owner-written core persona stays fixed. Learning proposes at most one 80-character habit per
review, requires evidence plus an independent consistency audit, and becomes less frequent as
conversation accumulates. Occasional feedback questions fit into normal replies; there are no idle
interviews. Review and revert accepted changes per channel in **Persona development**.

All AI features share a default **¥1 daily budget**, reset at local midnight in Australia/Sydney.
Set `DEEPSEEK_DAILY_BUDGET_CNY` and `DEEPSEEK_BUDGET_TIMEZONE` in `.env`. Requests reserve a
conservative maximum before sending, then settle reported usage with cache discounts. Official
peak rates verified on 2026-10-31 deliberately overestimate off-peak bills; the provider's bill
is authoritative. Unknown outcomes retain their charge. Daily count ceilings are optional (0: off).

Prompts keep stable history before changing time/retrieval/task data to support DeepSeek's
prefix cache. The dashboard displays actual cache hit/miss input tokens and output tokens;
cache availability and hit rate depend on the provider. Reply tool definitions also stay stable;
actual cache hit metrics remain authoritative. See the complete
[AI setup, lifecycle and rollout guide](docs/ai-chat-memory.md).

## Miao-chan voice music requests (optional)

Use `/join` or `/play` to bring Miao-chan into your voice channel, then speak a command. Voice commands use Chinese; the examples below keep the spoken phrases and explain their meaning in English.

- **喵酱喵酱，下一首歌** (“Miao-chan, Miao-chan, next song”): skip the current track.
- **喵酱喵酱，播放周杰伦的晴天** (“Miao-chan, Miao-chan, play Qing Tian by Jay Chou”): infer the song title, search for it, add it to the front, and switch to it immediately while preserving the rest of the queue.
- **喵酱喵酱，加入雨爱吧** (“Miao-chan, Miao-chan, add Yu Ai”): add the song to the end without interrupting playback. When idle, this only queues the song; say “播放” (“play”) or “继续播放” (“resume playback”) to start.
- **喵酱喵酱，暂停播放 / 继续播放** (“Miao-chan, Miao-chan, pause / resume playback”).

You can also say just “喵酱喵酱”, wait for the Klee-style “我在喵” (“I'm here, meow”) reply to finish, then give a command within 8 seconds. If the command is unclear, the bot asks you aloud to repeat it; a timeout ends the listening session. During music playback, the bot briefly lowers the music volume and restores it after replying. Replies while paused do not advance the music's playback position. See [Voice replies](docs/voice-replies.md) for the mechanism and voice sources. Audio from other bots is ignored, and the speaker must still be in Miao-chan's voice channel. Recognized commands appear in the text channel most recently used for music commands. Headphones help prevent music from feeding back through your microphone.

Wake detection combines phonetic detection with text matching, so homophones in the transcript do not prevent “喵酱” from being recognized. If a pause splits the two repetitions into separate segments, they can be joined within 3 seconds. Common polite phrases such as “喵酱喵酱，请帮我下一首歌吧” (“Miao-chan, Miao-chan, please skip to the next song”) are supported. If the wake phrase is detected but the following command is unclear, the bot prompts you aloud and lets you try the command again.

Spoken “播放某首歌” (“play a song”) uses `playskip`: it adds the song to the front and immediately skips the current track. “加入某首歌” (“add a song”) and “加入列表某首歌” (“add a song to the queue”) only append it. Song-title matching uses pinyin suggestions, common songs, the server's current queue and history, and your own favorites and playlists; for example, “与爱” can be corrected to “雨爱” (Yu Ai). Search compares multiple music results and gives unrelated lectures and short videos lower priority. Corrected search terms appear in the text channel. If the match is uncertain, the bot offers up to three candidates. Within 30 seconds, say “第一个”, “第二个”, or “第三个” (“the first”, “the second”, or “the third”), or say “取消” (“cancel”).

Use `/voice status` or `!voice` to check the status. Saying “闭嘴”, “取消”, or “安静” (“be quiet”, “cancel”, or “quiet”) ends your own listening session and candidate selection while music keeps playing. Text alternatives are `!voice quiet` and `/voice quiet`. Saying “喵酱喵酱” again clears the previous candidates, cancels the previous search, and starts a new command. Each wake session gives at most one “I didn't catch that” prompt; listening timeouts end silently. “播放歌单／列表／队列” (“play the playlist / list / queue”) starts the existing queue. Members with **Manage Server** permission can temporarily disable or enable voice commands for the server with `/voice off` and `/voice on`; restarting restores the default. To disable voice commands globally, set `VOICE_COMMANDS_ENABLED=false` in `.env` and restart.

For troubleshooting, use `!voice test` or `/voice test`. For the next 30 seconds, it accepts up to 3 test segments from the person who started the test, displaying the recognized text, phonetic wake-detection result, and audio-level metrics without saving recordings. Prefix-command diagnostics appear in the current channel; slash-command diagnostics are visible only to the person who invoked them. Ordinary recognition results and other members' transcripts are not shown as diagnostic output.

To install, run `python scripts/install_voice.py` from the project root. The installer selects the update environment currently used by the bot, or `.venv`. Download the [SenseVoice int8 model](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2), extract it into `data/voice-models/`, set `VOICE_COMMANDS_ENABLED=true`, and restart. The model directory must contain `model.int8.onnx` and `tokens.txt`; use `VOICE_MODEL_PATH` for a different location. Chinese commands use `VOICE_ASR_LANGUAGE=zh`, with two CPU threads for inference. This feature requires discord.py 2.7.1 and DAVE support. For source checkouts, you can also install `requirements-voice.txt` in the bot's Python environment; for Release ZIPs, use the installer above. Models must be downloaded separately. Regular music playback does not require the voice dependencies.

For more reliable wake detection, also download the [Chinese phonetic wake-word model](https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2) and extract it into `data/voice-models/`. You can specify a different directory with `VOICE_WAKE_MODEL_PATH`. Without this model, transcript-based wake matching still works. A status of `wake_detector=phonetic` means phonetic detection is enabled. The additional model is about 31 MiB and uses one CPU thread.

Linux also requires the system Opus library. On Debian, Ubuntu, or Raspberry Pi OS, run `sudo apt-get install libopus0`. If a native codec library is missing, the voice status displays an error and the logs provide details. Regular music playback continues through its existing workflow.

Audio is recognized in segments held locally in the Raspberry Pi's memory. Each segment is at most 12 seconds long, the queue holds at most 6 segments, and pending results older than 8 seconds are discarded. No cloud speech API is used, and neither recordings nor ordinary conversation transcripts are saved. Song requests and control feedback in the server use the existing music workflow. The `voice_commands` field in `runtime/status.json` provides receive, decode, and command counters for troubleshooting. Only audio from people in the bot's voice channel can be recognized; you cannot summon the bot by voice before it has joined a channel.

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

With this updater installed, the owner can also use `!update branch:chatbot` to install that
branch's current commit, or `!update COMMIT_SHA` / `!update commit:COMMIT_SHA` to pin a
7–40 character Git commit SHA. Branch names and commits always belong to the trusted repository.
The branch is resolved once, downloaded by its full SHA, and application files are verified
against its Git tree. These source builds do not require a Release. Bare `!update` always
selects the latest stable Release, including switching back from a source build at the same version.

Chat and memory **code** update together. Existing memory, chat records, model usage, settings
and credentials stay local. Before code replacement, AI SQLite databases are backed up under
`runtime/updates/<job-id>/data-backup/`; declared memory schema mismatches stop the update.
Rollback restores code and the Python environment while retaining live data. See
[source update details and first-install requirements](docs/releases.md#source-builds-branches-and-commits).

Run through `start.bat`, the tray, or `python main.py` so the restart supervisor is active. Install v1.1.1 or newer once using the manual instructions above; after that, use `!update`. Versions before v1.1.0 lack the command, and v1.1.0 needs the v1.1.1 download-header fix. Public releases must contain both the Windows ZIP and SHA-256 assets. Private repositories, prereleases and downgrades are not supported. If an update fails, check `logs/update.log` and `runtime/updates/<job-id>/`. Backups are kept there for manual recovery; keep this folder while an update is in progress. Restart the normal launcher to recover an interrupted installation.

Join a voice channel and use `/play` with a song name or URL. Leave the input empty to use your server's default playlist, or resume a saved queue. Use **Add song**, **Pause / Resume**, **Skip**, and **Favorite** on the Discord player. The player displays the original cover image, without a progress bar.

Use `/playtop input:song name or URL` or `!playtop song name or URL` to add music to the front of the waiting queue. The current track keeps playing, and paused playback stays paused. When idle, playback starts from the new track. Input is required; local MP3s and autocomplete work like `/play`. Playlists and collections are inserted as a block in their original order, subject to the existing duplicate and size limits. `/playtop` shares `/play`'s cooldown. To move a track already in the queue, use `/nextup index`.

Use `/playskip input:song name or URL` or `!playskip song name or URL` for **play → move to front → skip**. It resolves and queues the requested track or playlist first, then skips the current track. A failed search does not interrupt playback. Playlists keep their insertion order, the remaining queue is preserved, and a paused track is skipped too. When idle, the requested track starts once. Input and autocomplete work like `/playtop`.

The default playlist import limit and waiting queue capacity are both 1000 tracks. Each server can adjust these limits in the dashboard.

Each server has one player and one shared queue. `/join` moves the bot to your voice channel while keeping the current track, queue, playback position, volume and pause state. Moving the bot in Discord keeps the same player too. Other playback controls require you to share the bot's voice channel. When disconnected, `/resume` can restore the server queue in any voice channel.

The local dashboard is available at `http://127.0.0.1:8766/`. Controls, command descriptions and operation messages are in English. Welcome messages may remain in Chinese.

### New music tools

- **Queue:** `/queue page:2` or the Previous / Next buttons. The web dashboard supports search, pages, Play next, Remove, Shuffle, Fair queue and Remove duplicates. Stale web queue positions are rejected instead of changing the wrong track.
- **Your library:** `/favorite`, `/favorites`, `/unfavorite index:1`. `/playlist_save name:Chill` saves the current track plus queue; `/playlist_load name:Chill` appends it. `/playlist_load name:favorites` plays favorites. `/playlists` lists saved playlists; `/playlist_delete` removes one. Names autocomplete in Discord. Personal libraries are isolated by server and user and survive restarts.
- **Fair queue:** `/fair` takes turns by requester while preserving each person's order. `/dedupe` removes duplicate waiting tracks. `/nextup index:3` moves track 3 to the front. `/surprise` picks a random next track without interrupting the current song.
- **Sleep:** `/sleep minutes:30` pauses after 30 minutes and keeps the queue. `/sleep minutes:0` cancels the timer. Timers are checked every 15 seconds and persist across restarts.
- **Paused for 10 minutes:** disconnect automatically and save the current position and queue. The check runs every 30 seconds. `/resume` reconnects in your current voice channel; the dashboard resumes in the last voice channel when someone is there. Empty-channel timeout can disconnect earlier.
- **History:** `/history page:1` shows the latest 100 completed/skipped tracks in this session. The web dashboard shows the latest 20. History survives restart and automatic disconnection; explicit `/stop` clears the session.
- **Playback fixes:** volume changes apply immediately. Skip bypasses track repeat. `/replay` restarts without adding queue duplicates. `/seek +30` and `/seek -15` move relative to the current position.

See [the maintenance guide](docs/music-maintenance.md) for code ownership, storage, testing and common changes.

## Discord commands

`both` supports `/command` and `!command`; `!` supports prefix commands only; `/` supports slash commands only.

| Command | Support | Parameters | Description |
|---|---|---|---|
| `chat` | both | `mode: status, observe, on, off` | Show status or configure this channel's AI participation; changes require Manage Server |
| `memory` | both | `operation, memory_id, text` | List, remember, correct or forget your own channel-scoped memories |
| `replay` | both | None | Restart the current song |
| `fair` | both | None | Alternate requesters while preserving each user's order |
| `dedupe` | both | None | Remove duplicate waiting tracks |
| `nextup` | both | `index: int` | Move the selected track to the front of the queue |
| `surprise` | both | None | Choose a random next track without interrupting playback |
| `sleep` | both | `minutes: int = 0` | Schedule a pause and preserve the queue; 0 cancels |
| `history` | both | `page: int = 1` | Show recently played tracks in this server |
| `favorite` | both | None | Favorite the current song |
| `favorites` | both | `page: int = 1` | Show your favorites in this server |
| `unfavorite` | both | `index: int` | Remove a favorite by its index |
| `playlist_save` | both | `name: str` | Save the current track and queue as a personal playlist |
| `playlist_load` | both | `name: str` | Append a personal playlist; favorites loads your favorites |
| `playlists` | both | None | List your personal playlists |
| `playlist_delete` | both | `name: str` | Delete a personal playlist without interrupting playback |
| `join` | both | None | Join or move to your voice channel, keeping the server queue and current track |
| `playlist_defult` | ! | `url` | Set this server's default playlist; requires Manage Server. Example: `!playlist_defult https://music.youtube.com/playlist?list=PLH6zD0MCw2r4` |
| `welcome` | ! | Optional message or `on`, `off`, `status`, `reset`, `test` | Configure welcomes for this server; requires Manage Server; no text selects the current channel and enables welcomes |
| `seek` | both | `position` | Seek to a timestamp, such as `/seek 1:30` or `!seek 90`; 0 restarts |
| `poll` | both | `question options hours multiple` | Create a poll with pipe-separated options; defaults to 24 hours and single choice |
| `poll_end` | both | `message` | End a poll early using a message link or an ID from this channel; creator or administrator only |
| `play` | both | `input` | Play local MP3s, YouTube/Bilibili links, playlists or collections, or search keywords; `/play` supports autocomplete |
| `playtop` | both | `input` (required) | Add a song or playlist to the front of the waiting queue without interrupting playback; supports autocomplete |
| `playskip` | both | `input` (required) | Add a song or playlist to the front and skip the current track; supports autocomplete |
| `queue` | both | None | Show this server's queue |
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
| `voice` | both | Optional status/on/off/test/quiet | Local voice music commands; on/off requires Manage Server |
| `sync` | ! | None | Sync global slash commands; bot owner only |
| `update` | ! | Optional repository URL, `branch:NAME`, or commit SHA | Install a stable release or pinned source build and restart; bot owner only |
| `a` | ! | None | Alias for `!sync`; bot owner only |
| `emoji` | ! | None | Show configured emoji |
| `add` | ! | `a`, `b` | Add two integers |
| `help` | ! | Optional command name | List commands or show help for one command |

There are currently no slash-only commands. Player buttons use the same operations as commands.

### Welcome messages

Members with **Manage Server** permission can configure welcomes using prefix commands:

- `!welcome`: use the current text channel and enable welcomes, keeping the saved message (or the default).
- `!welcome Welcome {user} to {server}! You are member {count}.`: also save this custom message; multiline text is supported.
- `!welcome off` / `!welcome on`: disable or enable without changing the saved channel or message.
- `!welcome status`: show the enabled state, channel and message template.
- `!welcome reset`: restore the default welcome text, keeping the channel and enabled state.
- `!welcome test`: send a preview in the configured welcome channel using the command author as the new member, even when welcomes are disabled. This does not change settings or mark the member as already welcomed.

Templates support `{user}` (member mention), `{server}` (server name) and `{count}` (member count). Other braces remain literal. The default is the original Chinese welcome with member count when available. Templates are limited to 2000 characters (emoji may count as two); expanded messages are capped to Discord's limit. Only the welcomed member can be pinged, not roles or everyone. A single reserved word such as `off` is treated as an operation; otherwise the whole input is welcome text.

Settings are saved per server in `config/guilds/<guild-id>/welcome.json` and apply to future member events. The web dashboard can still change the channel and switches without replacing a custom message. The bot needs View Channel and Send Messages in the welcome channel. If member events were disabled at startup, enabling welcomes saves the settings and asks you to restart; enable **Server Members Intent** in the Discord developer portal. `!welcome` remains available while welcomes are disabled.

### Playback recovery and Discord player

- HTTP 403 download failures trigger a fresh page lookup and up to two additional attempts, with another audio format on the final attempt. Persistent failures skip the track.
- The server queue, current track, volume, repeat mode and position are stored in `data/queues/<guild-id>/queue.json`. Changes are saved immediately; playback position is checkpointed every 15 seconds. Restart does not automatically join voice. Use `/resume` or an empty `/play` in any voice channel to continue. A new queue's empty `/play` uses the server's default playlist.
- v1.3.0 merges older per-channel snapshots into one server queue, retaining the original files as backups. Back up `data/` before upgrading. See the [v1.3.0 upgrade notes](docs/release-notes/v1.3.0.md) for migration and API changes.
- `/stop` clears the server queue and disconnects; `/clear` only clears waiting tracks. An abrupt interruption may lose approximately 15 seconds of playback progress.
- One Discord message updates the current track, cover, next track and controls. Temporary network failures preserve the message and retry; deleted messages are recreated. The cover changes with each track. The Discord player does not display playback progress.

### Polls

Use `/poll` with a question and 2–10 options separated by `|`. `hours` accepts 1–768 and defaults to 24. Set `multiple` to true to allow multiple choices. Prefix example: `!poll "What is for dinner?" "Hot pot|Pizza|Noodles" 24 false`.

Use `/poll_end message:MESSAGE_LINK` or `!poll_end MESSAGE_LINK` to end a poll early. Only its creator or a member with Manage Server or Manage Messages in the channel can do this.

Discord native polls preserve votes and deadlines even while the bot is offline, and expire automatically. Creator and management records are stored per server in `data/polls/`. Polls are not anonymous; members can inspect votes through Discord. Both the bot and the creator need permission to send polls.

