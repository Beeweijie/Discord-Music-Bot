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
