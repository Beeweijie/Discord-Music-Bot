# Discord Music Bot

A Discord music bot built with `discord.py`. It supports local MP3 files, YouTube/Bilibili links, YouTube search, playlists/collections, queue controls, and member welcome messages.

## Project Structure

```text
Discord-Music-Bot/
├─ main.py                 # Bot entry point: loads extensions, syncs commands, starts the bot
├─ bot/
│  ├─ music.py             # Music playback, queue, download, search, and pre-download logic
│  ├─ welcome.py           # New member welcome events
│  └─ path.py              # Shared project path constants
├─ config/
│  ├─ emoji.json           # Emoji configuration
│  └─ music.json           # Reserved music configuration
├─ scripts/
│  ├─ emoji_create.py      # CLI helper for adding custom emoji
│  └─ start_bot.bat        # Windows startup script
└─ assets/
   └─ music/
      ├─ test.mp3          # Local music example
      └─ cache/            # Download cache for remote audio
```

## Music Commands

Music commands support both slash commands and `!` prefix commands:

- `/join` or `!join`: Join your current voice channel.
- `/play <input>` or `!play <input>`: Play a direct link, local MP3, or search keyword.
- `/play_list <url>` or `!play_list <url>`: Add a playlist/collection and shuffle it.
- `/queue` or `!queue`: Show the current queue.
- `/pause` or `!pause`: Pause the current track.
- `/resume` or `!resume`: Resume the current track.
- `/now` or `!now`: Show the currently playing track.
- `/remove <index>` or `!remove <index>`: Remove a queued track by index.
- `/volume <0-100>` or `!volume <0-100>`: Set the playback volume for the current voice session.
- `/shuffle` or `!shuffle`: Shuffle the current queue.
- `/skip` or `!skip`: Skip the current track.
- `/stop` or `!stop`: Stop playback, clear the queue, and leave the voice channel.

## `/play` Input Rules

- If the input starts with `http://` or `https://`, the bot parses it as a direct link.
- If the input is not a link and `assets/music/<input>.mp3` exists, the bot plays the local file.
- If the input is not a link and no matching local file exists, the bot searches YouTube and plays the first result.
- Slash command input autocomplete suggests local MP3 files and YouTube search results.

Examples:

```text
!play test
!play https://www.youtube.com/watch?v=...
!play daoxiang jay chou
```

## Run Locally

1. Add your Discord bot token to `.env`:

```env
DISCORD_TOKEN=your_token_here
```

2. Install FFmpeg. On Windows, the current code expects:

```text
C:/Program Files/ffmpeg/bin/ffmpeg.exe
```

3. Start the bot:

```bash
python main.py
```

## Windows Auto Start

The script below can be linked from the Windows Startup folder to start the bot when you log in:

```text
scripts/start_bot.bat
```

Logs are written to:

```text
logs/startup.log
logs/bot.log
```
