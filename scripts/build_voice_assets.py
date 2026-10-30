"""Encode the five fixed replies as source accepted by existing update clients."""
import base64
import textwrap
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build():
    folder = ROOT / 'bot_app/features/music/media'
    lines = ['"""Generated fixed replies; rebuild with scripts/build_voice_assets.py.',
             'Voice/model attribution: docs/voice-replies.md. No microphone recordings.',
             '"""', 'import base64', 'import zlib', '', 'CLIPS = {']
    for name in ('wake', 'ok', 'unclear', 'timeout', 'choices'):
        data = base64.b64encode(zlib.compress((folder / 'replies' / (name + '.wav')).read_bytes(), 9)).decode('ascii')
        lines.append(f'    {name!r}: (')
        lines.extend('        ' + repr(line) for line in textwrap.wrap(data, 100))
        lines.append('    ),')
    lines += ['}', '', '', 'def clip_bytes(name):',
              '    return zlib.decompress(base64.b64decode(CLIPS[name]))', '']
    (folder / 'voice_assets.py').write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    build()
