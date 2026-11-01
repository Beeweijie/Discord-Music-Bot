# Releases and updates

## Stable version and abandoned experiment

v1.3.0 is the latest stable GitHub Release. Its original tag and downloadable assets are
retained. `main` restores its music behavior while retaining feature-first organization
and pinned branch/commit updates. The historical Release ZIP predates these source-only
maintenance changes; install current main source to use the new updater and architecture.

v1.4.0 is **abandoned / 废案**. Its tag, notes and assets remain available for reference,
but the Release is marked as a prerelease and must not be selected by automatic stable
updates. Optional 海洋 voice control and `playskip` now live on `codex/haiyang-voice`.
Bot chat and related AI features are absent from both active branches.

## Verification and packaging

Use Python 3.13 and install `requirements.txt`. Run the complete suite:

```text
python -m unittest discover -s tests -v
python main.py --check
python scripts/build_release.py v1.3.0
```

`--check` validates local configuration and FFmpeg without logging in to Discord. Do not
start a second bot against an active installation's data. Tests cover command registration,
music, queue recovery, management, desktop control, welcome, polls, archive validation,
real supervisor restarts and rollback. GitHub CI runs the full suite on Linux and Windows.
The voice branch additionally runs before and after installing optional voice dependencies.
Real Discord audio and microphone recognition require separate live checks.

The builder uses an explicit public-file allowlist and produces a deterministic ZIP and
SHA-256 checksum under `dist/`. Credentials, per-server settings, data, runtime files, logs,
environments, caches and development artifacts are excluded. Do not recursively zip the
working directory. Source manifests and packaged manifests list their actual public files.

## Release updates

The owner-only `!update` command selects the trusted repository's latest stable Release.
`UPDATE_GITHUB_URL` defaults to this project. A supplied repository URL must match that
configured repository. A prerelease or incomplete ZIP/checksum upload is rejected.

Preparation verifies checksum/API digest, manifest, paths, size and file counts before
installing dependencies in a separate environment. It does not stop the running bot until
preparation succeeds. Queues are saved before the supervised restart. The candidate must
pass configuration checks and become ready within 90 seconds; otherwise the previous code
and environment are restored. Saved queues require `/resume` after restart.

The updater accepts old layer-based packages and current feature packages. It removes
obsolete application files recorded by the prior manifest. `.env`, `config/`, `assets/`,
`data/`, the original `.venv` and runtime recovery records are protected from replacement.
Retired chat data is left untouched and is not opened by this version.

Preparation, check and recovery logs remain in `runtime/updates/<job-id>/` and
`logs/update.log`. Do not remove a pending update directory. A crash during replacement
is repaired on the next normal launch using the standalone recovery helper.

## Pinned source updates

Install current main source once if upgrading from the original v1.3.0 Release, whose
updater understands Releases only. Then the owner can use:

```text
!update                              latest stable Release
!update branch:main                  current maintained stable source
!update branch:codex/haiyang-voice    optional voice experiment
!update COMMIT_SHA                    7–40 hexadecimal characters
!update commit:COMMIT_SHA             explicit pinned commit
```

A branch is resolved once to a full immutable SHA. The updater downloads that commit's
archive and verifies each selected application file against its Git blob hash. Truncated
trees, missing updater/launcher files, unsupported protocols, content changes, links,
traversal, duplicate paths and oversized archives are rejected before activation.

The installed manifest records `source.kind`, `source.ref`, `source.commit` and
`source.repository`. Messages include the short SHA. Installing the same commit is a
no-op; readiness requires the exact staged SHA. Selecting an older commit is an explicit
source rollback. Source updates use the same staging, supervisor and rollback as Releases.

Bare `!update` returns a source installation to the latest stable Release at the same
base version. It does not track the selected branch and does not automatically downgrade
an installed v1.4.0 Release; migrate that version manually or select current main using a
source-capable updater. Returning to the historical v1.3.0 ZIP also restores its older updater.
