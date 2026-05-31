# Discord Music Bot

A Discord music bot built with `discord.py`. It supports local MP3 files, YouTube/Bilibili links, YouTube search, playlists/collections, queue controls, and member welcome messages.

## Project Structure

```text
Discord-Music-Bot/
|-- main.py                 # Bot entry point: loads extensions, syncs commands, starts the bot
|-- requirements.txt        # Python dependencies
|-- .env.example            # Example environment file
|-- bot/
|   |-- music.py            # Music playback, queue, download, search, and pre-download logic
|   |-- welcome.py          # New member welcome events
|   `-- path.py             # Shared project path constants
|-- config/
|   |-- emoji.json          # Emoji configuration
|   `-- music.json          # Reserved music configuration
|-- scripts/
|   |-- setup_windows.bat   # One-click Windows setup wrapper
|   |-- setup_windows.ps1   # Installs Python/FFmpeg/dependencies
|   |-- start_bot.bat       # Starts the bot and writes logs
|   |-- install_startup.ps1 # Enables Windows login auto-start
|   `-- uninstall_startup.ps1
`-- assets/
    `-- music/
        |-- test.mp3        # Local music example
        `-- cache/          # Download cache for remote audio
```

## Configuration

Create a `.env` file in the project root. You can copy `.env.example`:

```env
DISCORD_TOKEN=your_discord_bot_token_here
```

Required Discord Developer Portal settings:

- Enable the bot token in the Bot page.
- Enable `MESSAGE CONTENT INTENT` if you want `!` prefix commands.
- Invite the bot with both scopes: `bot` and `applications.commands`.
- The current code syncs slash commands to the guild ID configured in `main.py`.

FFmpeg is required for voice playback. On Windows, the current code first expects:

```text
C:/Program Files/ffmpeg/bin/ffmpeg.exe
```

If your FFmpeg is elsewhere, update `self.ffmpeg_path` in `bot/music.py`.

## One-Click Windows Setup

Run this from the project root:

```powershell
scripts\setup_windows.bat
```

The setup script will:

- Try to install Python 3.14 with `winget`.
- Fall back to Python 3.13 if Python 3.14 is unavailable.
- Try to install FFmpeg with `winget`.
- Create a `.venv` virtual environment.
- Install all Python dependencies from `requirements.txt`.
- Create `.env` from `.env.example` if `.env` does not exist.

To also enable Windows login auto-start during setup:

```powershell
scripts\setup_windows.bat -InstallStartup
```

If `winget` is unavailable, install Python and FFmpeg manually, then rerun the setup script.

## Manual Install

```bash
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

Then edit `.env` and start the bot:

```bash
scripts\start_bot.bat
```

## Windows Auto Start

To start the bot automatically when you log in to Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
```

To disable auto-start:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall_startup.ps1
```

The startup shortcut points to:

```text
scripts/start_bot.bat
```

Logs are written to:

```text
logs/startup.log
logs/bot.log
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

## Queue and Playlist Limits

- The maximum pending queue size is `200` tracks per voice channel.
- A playlist/collection is de-duplicated before being added.
- If a playlist has more than `100` unique tracks, the bot randomly selects `100` of them.
- The bot pre-downloads up to `3` upcoming remote tracks.
- Remote audio is stored in `assets/music/cache/`, and old cache files are deleted when the bot starts.
