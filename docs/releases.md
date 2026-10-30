# Publishing a release

Release ZIPs contain source and Windows launchers. Python and FFmpeg are installed
on the user's computer by `install.bat`; this is not a standalone executable.

Before publishing, install `requirements.txt` into a clean Python environment and
run `python -m unittest discover -s tests`. Run `python main.py --check` locally
when configured; it does not connect to Discord. Do not start another bot against
an active installation's data.

For v1.4.0, use the [release notes](release-notes/v1.4.0.md). Verify an upgrade
from v1.2.1 with multiple saved channel queues, including a restart after Stop.
In a test Discord voice channel, verify playback and paused moves from A to B
and back, controls in the destination, and rejection of controls from a different
channel. These live voice checks supplement the mocked regression suite.

Build with `python scripts/build_release.py v1.0.0`. The script uses an explicit
allowlist and outputs a reproducible ZIP plus its SHA-256 checksum in `dist/`.
It excludes `.env`, per-server configuration, runtime files, user libraries,
queues, logs, environments and caches. Test the ZIP after extracting it to a path
with spaces: run `install.bat`, configure a test token, and check tray startup.
`install.bat -NoConfigure` supports dependency-only smoke tests without a token.

Commit the release source, tag it as `vX.Y.Z`, and push the commit and tag. The
release workflow installs dependencies, runs the suite on Linux and Windows,
builds the ZIP and uploads both assets to a draft before publishing it. It uses
`docs/release-notes/<tag>.md` when present. It can also be run manually against
an existing tag. Failed tests prevent that workflow from publishing assets.
Never publish files by recursively zipping the working directory.

Voice commands are optional. The release embeds the fixed reply audio in a Python
module so existing v1.3.0 updater path checks accept the ZIP. Original WAVs and
`requirements-voice.txt` stay in the source repository; the ZIP includes
`scripts/install_voice.py` to install dependencies into the selected bot environment.
ASR and keyword models are downloaded separately and stored under `data/voice-models/`.
From v1.4.0 onward, update preparation installs the optional dependencies when voice
commands are enabled. An upgrade using an older updater prepares base dependencies
only; run the helper after that first upgrade if voice was previously enabled.

## Automatic updates

Starting with v1.1.1, the owner-only prefix command `!update [GitHub repository URL]`
uses GitHub's latest stable release endpoint. The URL must match the locally
configured `UPDATE_GITHUB_URL`; the default is this project's public repository.
Both uploaded assets must exist before an update can begin. An incomplete release
is rejected and can be retried after publishing finishes.

`version.json` records a stable version and `update_schema: 1`. The builder fills
its public file manifest from the distribution allowlist and stamps the actual
tag. Keep this schema compatible with the existing supervisor. Updates never
write `.env`, `config/`, `assets/`, `data/` or the original `.venv`. They replace
only allowed application files and remove obsolete files from the previous
release manifest. Locally modified application code is backed up and replaced.

Preparation verifies archive/checksum/API digest, paths, file counts and size,
creates a separate dependency environment, and smoke-imports the new code while
the existing bot stays online. Once the owner receives the restart message, the
bot saves its queues and exits with code 75. The supervisor holds a separate
installation lock, backs up application files, persists a rollback journal,
replaces code, checks configuration, and launches the candidate environment.
The candidate must connect to Discord and mark ready within 90 seconds. Only
then does the supervisor commit and arrange a completion message in the request
channel. Saved queues require explicit `/resume` after restart.

A failed configuration check, early exit or readiness timeout restores code and
the previous environment pointer. A crash during replacement is repaired on the
next normal launch, using a standalone copy of the previously working updater.
Recovery journals, environments and backups remain in `runtime/updates/<id>/`.
Do not remove that directory while an update is pending. Details are in
`logs/update.log`, `prepare.log` and `check.log` within the job folder. Dependency
installation has a ten-minute timeout. Failed downloads/preparation never stop
the running bot. Upgrading a release older than v1.1.1 requires a manual install
once so the supervisor, command and binary download-header fix are available.

## Source builds: branches and commits

An owner running this updater can use:

```text
!update                      latest stable Release
!update branch:chatbot        the chatbot branch's current commit
!update branch:feature/name   a branch containing a slash
!update COMMIT_SHA            a 7–40 character hexadecimal commit SHA
!update commit:COMMIT_SHA     the same explicit pinned-commit operation
```

Only the repository configured by `UPDATE_GITHUB_URL` is accepted. The optional repository
URL form continues to select Releases. A branch name and a commit ID are different: a branch
moves as new commits are pushed; a commit identifies one revision. Each branch update resolves
its head once, records the full SHA and downloads that immutable source archive. A repeated
update to the same installed commit is a no-op. Selecting an older commit is an explicit source
rollback; there is no automatic semantic-version downgrade through the Release channel.

The updater retrieves the pinned commit's complete Git tree and verifies every selected
application file using its Git blob SHA before preparing dependencies. It rejects incomplete
trees, missing required files, unsupported update protocols, mismatched content, links,
traversal, duplicate paths and oversized archives. Repository source archives contain extra
files such as workflows, WAVs and configuration examples; only the updater's application
allowlist is installed. `.env`, actual configuration, assets and `data/` are never installed
from the source archive. Optional voice dependencies retain the installer workflow.

The staged `version.json` retains the base stable version and adds `source.kind`, `source.ref`,
`source.commit` and `source.repository`. Preparation and completion messages display the
branch/commit and short SHA. New code must mark the exact staged commit ready. Source builds
share the same environment staging, configuration check, restart deadline and code rollback as
Release updates. Bare `!update` always returns to the latest stable Release; it can replace a
source build at the same base version. Choosing a source build is explicit and does not make
future bare updates track that branch.

Chat and memory code update as one application. Freezing only the memory implementation would
risk incompatible calls between versions; retaining the local database is the compatibility
boundary. The new manifest declares `data_schemas.memory: 1`. If an existing memory database's
schema differs from a declared target schema, preparation/apply refuse the update without
changing that database. This version does not automatically migrate or downgrade memory data.
Older versions without the AI feature do not declare or open this store.

After the bot stops and before replacing application code, the supervisor uses SQLite's backup
API to snapshot any existing memory, chat and usage databases into
`runtime/updates/<job-id>/data-backup/data/{memory,chat,ai}/`. Backup failure prevents code
replacement. Local database files are not part of the code replacement or code rollback lists.
Rollback preserves current data rather than overwriting it with the snapshot, since new memory
could have arrived after startup. If manual data recovery is required, stop the bot and choose
a compatible backup deliberately. Backups may contain the same private information as live
memory; they remain local and are excluded from Git and release packages.

**First installation:** an old Release-only updater cannot understand the new command before
it is installed. Install this branch/version once using the existing source/manual installation
workflow, retaining `.env`, `config/` and `data/`, then restart through the normal supervisor.
After that, source updates can be requested from Discord. Pushing a branch alone does not add
new command parsing to a bot that is still running old code.

## Linux updates failing before code replacement

Versions through v1.2.0 can reject a normal Linux virtual environment with
`The prepared Python environment is missing or outside the update directory.`
The executable `environment/bin/python` is normally a symbolic link to system
Python. The old check resolves that final link and incorrectly treats the system
interpreter as an escaping update path. v1.2.1 resolves only the containing
directory, keeps the venv launch path, and still rejects missing executables and
directories that point outside the update storage.

An affected old updater fails before installing the fix, so retrying `!update`
alone cannot repair it. Stop the bot's usual launcher/service, then run this
one-time repair in the existing installation (replace the root path as needed):

```bash
curl -fL https://raw.githubusercontent.com/Beeweijie/Discord-Music-Bot/v1.2.1/scripts/fix_linux_update.py -o /tmp/fix_linux_update.py
python3 /tmp/fix_linux_update.py --root /home/hche616/Discord-Music-Bot
```

The helper refuses a running launcher or an unexpected updater, backs up the
original source, and changes only the faulty path check. It does not remove update
environments or edit credentials, settings or saved queues. Restart the same
launcher/service, then use `!update` to install the fixed release. Use `/resume`
after a successful upgrade. Future releases must pass real Linux venv/update and
rollback tests as well as the Windows release suite before assets are published.
