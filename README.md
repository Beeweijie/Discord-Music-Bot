# Discord Music Bot

## Windows 快速安装

1. 从 [GitHub Releases](https://github.com/Beeweijie/Discord-Music-Bot/releases/latest) 下载 `Discord-Music-Bot-*-windows.zip`，**完整解压**到长期保留的目录（不要在压缩包内运行）。
2. 双击根目录的 **`install.bat`**。脚本自动检测或安装 Python 3.13、FFmpeg，创建独立环境并安装依赖。首次安装需要联网；缺少 winget 时请先从 Microsoft Store 安装 **App Installer**，或手动安装 Python 3.13 和 FFmpeg。
3. 首次安装会打开 `.env`：将 `DISCORD_TOKEN=your_token_here` 替换成自己的 Bot Token，保存并关闭记事本。安装程序会检查配置，不连接 Discord。
4. 双击 **`start.bat`** 启动托盘应用。在托盘菜单中启动 Bot、打开网页控制台或设置开机启动。管理界面默认地址为 `http://127.0.0.1:8766/`。

在 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用，在 **Bot** 页面获取 Token，并启用 **Server Members Intent** 和 **Message Content Intent**。邀请应用时选择 `bot` 和 `applications.commands`，授予查看频道、发送消息、嵌入链接、连接和说话权限；使用投票还需发送投票权限。Token 只保存在本机 `.env`，不要上传或分享。

升级前从托盘退出旧版并备份 `.env`、`config/guilds/` 和 `data/`。解压新版到新目录，将这些文件复制过去，再运行 `install.bat`。若使用开机启动，请在新版托盘中重新设置快捷方式。安装程序保留已有 `.env`；发布包不包含账号、服务器数据或缓存。

高级用法：`install.bat -NoConfigure` 只安装依赖，稍后手动填写 `.env`；`-InstallStartup` 创建登录启动快捷方式。源码用户同样可以使用上述两个根目录入口。Windows 安装脚本支持 Python 3.12–3.14。

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

## Discord 命令

`both` 表示同时支持 `/命令` 和 `!命令`；`!` 表示仅支持前缀命令；`/` 表示仅支持 slash 命令。

| 命令 | 支持 | 参数 | 功能 |
|---|---|---|---|
| `replay` | both | 无 | 从头重播当前歌曲 |
| `fair` | both | 无 | 按点歌人轮流排列等待队列 |
| `dedupe` | both | 无 | 移除等待队列中的重复歌曲 |
| `nextup` | both | `index: int` | 把指定编号移到下一首 |
| `surprise` | both | 无 | 从等待队列随机抽取下一首，不打断当前歌曲 |
| `sleep` | both | `minutes: int = 0` | 定时暂停并保留队列；0 取消 |
| `history` | both | `page: int = 1` | 查看本频道最近播放的歌曲 |
| `favorite` | both | 无 | 收藏当前歌曲 |
| `favorites` | both | `page: int = 1` | 查看自己在本服务器的收藏 |
| `unfavorite` | both | `index: int` | 按收藏编号取消收藏 |
| `playlist_save` | both | `name: str` | 把当前曲目和队列保存为个人歌单 |
| `playlist_load` | both | `name: str` | 添加个人歌单；favorites 表示收藏 |
| `playlists` | both | 无 | 查看自己的个人歌单 |
| `playlist_delete` | both | `name: str` | 删除个人歌单，不影响正在播放 |
| `join` | both | 无 | 加入你所在的语音频道 |
| `playlist_defult` | ! | `url` | 修改本服务器的默认歌单，需管理服务器权限；例如 `!playlist_defult https://music.youtube.com/playlist?list=PLH6zD0MCw2r4` |
| `seek` | both | `position` | 跳到指定分秒，例如 `/seek 1:30` 或 `!seek 90`，`0` 从头播放 |
| `poll` | both | `question options hours multiple` | 创建投票，选项用 `\|` 分隔；默认 24 小时、单选 |
| `poll_end` | both | `message` | 发起人或管理员提前结束投票，填写消息链接或本频道消息 ID |
| `play` | both | `input` | 播放本地 MP3、YouTube/Bilibili 链接、播放列表/合集，或搜索关键词；`/play` 支持自动补全 |
| `queue` | both | 无 | 查看当前语音频道的播放队列 |
| `pause` | both | 无 | 暂停当前歌曲 |
| `resume` | both | 无 | 继续播放 |
| `now` | both | 无 | 查看当前歌曲和播放控制面板 |
| `remove` | both | `index` | 移除队列中指定编号的歌曲，编号从 1 开始 |
| `volume` | both | `volume` | 设置当前会话音量，范围 0–100 |
| `shuffle` | both | 无 | 打乱等待队列 |
| `skip` | both | 无 | 跳过当前歌曲；空闲时尝试播放下一首 |
| `stop` | both | 无 | 停止播放、清空队列并离开语音频道 |
| `clear` | both | 无 | 清空等待队列，保留当前歌曲 |
| `move` | both | `from_index`、`to_index` | 调整队列顺序，编号从 1 开始 |
| `loop` | both | `mode` | 设置循环模式：`off` 关闭、`one` 单曲、`queue` 队列 |
| `help_music` | both | 无 | 查看音乐命令使用说明 |
| `ping` | both | 无 | 查询 Bot 的 Discord 延迟 |
| `sync` | ! | 无 | 同步全局 slash 命令，仅 Bot 所有者可用 |
| `a` | ! | 无 | `!sync` 的别名，同样仅 Bot 所有者可用 |
| `emoji` | ! | 无 | 显示配置中的表情 |
| `add` | ! | `a`、`b` | 计算两个整数之和 |
| `help` | ! | 可选命令名 | 查看命令列表或指定命令的帮助 |

目前没有仅支持 `/` 的命令。播放面板的按钮与命令共用同一套操作逻辑。

### 播放恢复与 Discord 面板

- 下载遇到 HTTP 403 时会重新解析歌曲页面，最多额外重试两次，最后一次尝试其他音频格式。仍失败则跳过，不无限重试。
- 队列、当前曲目、音量、循环模式和播放位置存放在 `data/queues/<服务器ID>/<语音频道ID>.json`；操作时保存，播放位置每 15 秒记录一次。重启后不自动加入语音；在原频道使用 `/resume` 或不填参数的 `/play` 继续。新队列的空参数 /play 仍使用服务器默认歌单。
- 显式停止/清空会清除相应队列；异常中断可能损失最近约 15 秒播放进度。
- Discord 使用同一条消息更新当前曲目、歌曲封面、下一首与按钮；瞬时网络错误会保留消息并重试，消息被删除后重建。封面随歌曲切换；Discord 面板不显示播放进度。

### 投票

使用 `/poll`，填写问题和用 `|` 分隔的 2–10 个选项。`hours` 为 1–768 小时，默认 24；`multiple` 为 true 时允许多选。前缀用法：`!poll "晚饭吃什么" "火锅|披萨|面条" 24 false`。

`/poll_end message:消息链接` 或 `!poll_end 消息链接` 可提前结束，仅发起人或有管理服务器/该频道管理消息权限的成员可用。

票数和截止时间由 Discord 原生投票保存，即使 Bot 离线也能继续投票并自动到期。发起人等管理记录按服务器保存在 `data/polls/`。投票不是匿名投票；成员可通过 Discord 查看投票情况。Bot 与发起人都需有发送投票权限。
