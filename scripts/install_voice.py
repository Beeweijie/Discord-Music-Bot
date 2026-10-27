"""Install optional voice dependencies in the bot's selected Python environment."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPENDENCIES = ('discord.py[voice]==2.7.1', 'discord-ext-voice-recv==0.5.2a179',
                'sherpa-onnx==2.0.1', 'numpy>=2,<3', 'pypinyin==0.55.0')


def bot_python(root=ROOT):
    # Automatic updates select a separate environment. Installing into the
    # original .venv would otherwise leave the running bot without voice deps.
    sys.path.insert(0, str(root))
    from bot_app.infrastructure.updates import GitHubUpdater
    venv = root / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    fallback = str(venv) if venv.is_file() else sys.executable
    return GitHubUpdater(root).selected_python(fallback)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    python = bot_python()
    print('Installing voice dependencies into:', python, flush=True)
    subprocess.run([python, '-m', 'pip', 'install', *DEPENDENCIES], check=True)
    print('Install the ASR model, set VOICE_COMMANDS_ENABLED=true, then restart the bot. See README.md.')


if __name__ == '__main__':
    main()
