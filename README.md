# Discord-Music-Bot

一个基于 `discord.py` 的 Discord 音乐 Bot，支持本地 mp3、YouTube/Bilibili 链接、YouTube 搜索、播放列表/合集、队列管理和新成员欢迎消息。

## 项目结构

```text
Discord-Music-Bot/
├─ main.py                 # Bot 启动入口：加载扩展、同步命令、启动服务
├─ bot/
│  ├─ music.py             # 音乐播放、队列、下载、搜索、预下载逻辑
│  ├─ welcome.py           # 新成员欢迎事件
│  └─ path.py              # 项目路径常量
├─ config/
│  ├─ emoji.json           # 表情配置
│  └─ music.json           # 预留音乐配置
├─ scripts/
│  └─ emoji_create.py      # 命令行添加自定义表情
└─ assets/
   └─ music/
      ├─ test.mp3          # 本地音乐示例
      └─ cache/            # 远程音频下载缓存
```

## 音乐命令

音乐命令同时支持 slash commands 和 `!` 前缀命令：

- `/join` 或 `!join`：让 Bot 加入你所在的语音频道。
- `/play <input>` 或 `!play <input>`：播放单曲链接、本地 mp3，或搜索关键词。
- `/play_list <url>` 或 `!play_list <url>`：添加播放列表/合集并随机打乱。
- `/queue` 或 `!queue`：查看当前播放队列。
- `/pause` 或 `!pause`：暂停当前歌曲。
- `/resume` 或 `!resume`：继续播放当前歌曲。
- `/now` 或 `!now`：查看当前正在播放的歌曲。
- `/remove <index>` 或 `!remove <index>`：从队列移除指定编号的歌曲。
- `/volume <0-100>` 或 `!volume <0-100>`：设置当前语音频道的播放音量。
- `/shuffle` 或 `!shuffle`：打乱当前播放队列。
- `/skip` 或 `!skip`：跳过当前歌曲。
- `/stop` 或 `!stop`：停止播放、清空队列并离开语音频道。

`/play` 的输入规则：

- 如果是 `http://` 或 `https://` 链接，就直接解析链接。
- 如果不是链接，并且 `assets/music/输入.mp3` 存在，就播放本地文件。
- 如果不是链接，并且本地文件不存在，就用 YouTube 搜索第一条结果播放。
- 使用 `/play` 时，`input` 会自动补全本地 mp3 和 YouTube 搜索候选。

例子：

```text
!play test
!play https://www.youtube.com/watch?v=...
!play 稻香 周杰伦
```

## 运行

1. 在 `.env` 中配置 `DISCORD_TOKEN`。
2. 确保已安装 FFmpeg，并且 Windows 下路径为 `C:/Program Files/ffmpeg/bin/ffmpeg.exe`。
3. 运行：

```bash
python main.py
```
