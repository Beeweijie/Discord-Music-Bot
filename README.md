# Discord Music Bot

## 喵酱语音点歌（可选）

让喵酱通过 `/join` 或 `/play` 加入自己的语音频道，然后说：

- **喵酱喵酱，下一首歌**：跳过当前歌曲。
- **喵酱喵酱，播放周杰伦的晴天**：联想歌名并搜索，加入队首后立即切过去，保留其余待播队列。
- **喵酱喵酱，加入雨爱吧**：加入队尾，不打断当前歌曲；空闲时只排队，等你说“播放”或“继续播放”。
- **喵酱喵酱，暂停播放 / 继续播放**。

可以先单独说“喵酱喵酱”，等可莉角色音色的“我在喵”回复结束，再于 8 秒内说指令。没听清会出声提示重说；超时会结束本次等待。播歌时短暂降低音乐音量，回复后恢复；暂停时回复不推进音乐进度。具体机制和音色来源见 [语音回复](docs/voice-replies.md)。其他机器人的音频不作为口令；说话人必须仍在喵酱所在的频道。识别到的指令会显示在最近使用音乐命令的文字频道。使用耳机可减少音乐从麦克风回传带来的干扰。

唤醒同时使用读音检测和文字匹配，避免“喵酱”转成同音字后失效。两声“喵酱”被停顿拆成两段时，可在 3 秒内衔接；支持“喵酱喵酱，请帮我下一首歌吧”等常见说法。如果听到唤醒词但没听清后面的命令，会出声提示，可以接着再说一次指令。

语音“播放某首歌”使用 `playskip`，加入队首后立即跳过当前歌曲；“加入某首歌”“加入列表某首歌”只加入队尾。歌名使用拼音联想，结合常见歌曲、当前服务器队列、历史和本人的收藏/歌单，例如“与爱”→“雨爱”。搜索会比较多个音乐结果，降低无关讲座和短视频的优先级；纠正后的关键词会显示在文字频道。无法确定时列出最多三个候选，可在 30 秒内说“第一个”“第二个”或“第三个”，也可说“取消”。

`/voice status` 或 `!voice` 查看状态。说「闭嘴」「取消」「安静」可结束本人的监听和候选选择，音乐继续播放；文字兜底为 `!voice quiet` 或 `/voice quiet`。重新喊「喵酱喵酱」会退出旧候选、取消旧搜索，开始新指令。每次唤醒最多提醒一次「没听清」，监听超时安静退出。「播放歌单／列表／队列」播放现有队列。具有“管理服务器”权限的成员可用 `/voice off`、`/voice on` 临时关闭或开启本服务器语音指令（重启后恢复默认）。全局关闭可设置 `.env` 中的 `VOICE_COMMANDS_ENABLED=false` 后重启。

排障可用 `!voice test` 或 `/voice test`：仅接收发起者接下来 30 秒、最多 3 段测试转写，显示识别文字、读音唤醒结果和音量指标，不保存录音。前缀命令的诊断结果发在本频道，斜线命令仅发起者可见；普通识别和其他成员的转写不会作为诊断结果显示。

安装：在项目根目录执行 `python scripts/install_voice.py`，安装器会选择 Bot 当前使用的更新环境或 `.venv`。下载 [SenseVoice int8 模型](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2)，解压到 `data/voice-models/`，设置 `VOICE_COMMANDS_ENABLED=true` 并重启。模型目录应包含 `model.int8.onnx` 和 `tokens.txt`；其他位置可通过 `VOICE_MODEL_PATH` 指定。中文指令使用 `VOICE_ASR_LANGUAGE=zh`，推理使用两个 CPU 线程。功能需要 discord.py 2.7.1 和 DAVE 支持；源码检出也可在 Bot 的 Python 环境中安装 `requirements-voice.txt`，Release ZIP 使用上述安装器。模型需单独下载，普通音乐功能无需安装语音依赖。

为增强唤醒识别，再下载 [中文读音唤醒模型](https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2)，同样解压到 `data/voice-models/`。也可通过 `VOICE_WAKE_MODEL_PATH` 指定目录。没有该模型时仍可使用文字唤醒；状态中的 `wake_detector=phonetic` 表示读音检测已经启用。额外模型约 31 MiB，使用一个 CPU 线程。

音频在树莓派本地内存中分段识别，最长每段 12 秒，队列最多 6 段，超过 8 秒的待处理结果丢弃。没有云端语音 API，不保存录音或普通聊天文字；服务器内发出的点歌和控制反馈使用现有音乐流程。`runtime/status.json` 中的 `voice_commands` 提供接收、解码与命令计数，便于排障。只有与机器人同频道的声音可被识别；机器人尚未入频道时不能用喊话召唤。

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
| `update` | ! | Optional GitHub repository URL | Install the latest stable release and restart; bot owner only |
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
