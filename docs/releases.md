# Publishing a release

Release ZIPs contain source and Windows launchers. Python and FFmpeg are installed
on the user's computer by `install.bat`; this is not a standalone executable.

Before publishing, install `requirements.txt` into a clean Python environment and
run `python -m unittest discover -s tests`. Run `python main.py --check` locally
when configured; it does not connect to Discord. Do not start another bot against
an active installation's data.

Build with `python scripts/build_release.py v1.0.0`. The script uses an explicit
allowlist and outputs a reproducible ZIP plus its SHA-256 checksum in `dist/`.
It excludes `.env`, per-server configuration, runtime files, user libraries,
queues, logs, environments and caches. Test the ZIP after extracting it to a path
with spaces: run `install.bat`, configure a test token, and check tray startup.
`install.bat -NoConfigure` supports dependency-only smoke tests without a token.

Commit the release source, tag it as `vX.Y.Z`, and push the commit and tag. The
Windows release workflow installs dependencies, runs the suite, builds the ZIP
and publishes both assets to GitHub Releases. It can also be run manually against
an existing tag. Failed tests prevent that workflow from publishing assets.
Never publish files by recursively zipping the working directory.

## Automatic updates

Starting with v1.1.0, the owner-only prefix command `!update [GitHub repository URL]`
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
the running bot. Upgrading a release older than v1.1.0 requires a manual install
once so the supervisor and command are available.
