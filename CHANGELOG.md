# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is below
1.0, a minor release may change behaviour.

Each release is published as a container image:
`ghcr.io/m8-t/navidrome-wiim-bridge:<version>`.

## [0.3.0] - 2026-10-11

### Changed (breaking)

- The WiiM's address and the address the WiiM uses to reach the bridge no longer have
  built-in defaults. They must be set, with `BRIDGE_DEVICE_HOST` and
  `BRIDGE_ADVERTISE_HOST` or the matching options. The old defaults were the author's
  own network addresses. A missing setting is named in the error and the bridge exits.
- The request log file is off by default (`/dev/null`), and the helper file with the
  base address is only written when `--info-file` is given. The container image no
  longer passes any arguments by default.

### Added

- Every setting can be given as an environment variable. The command line wins over
  the environment, the environment over the default.
- `BRIDGE_DEVICE_HOST` / `--device-host`: the WiiM's address in one value. The two
  device addresses are derived from it and can still be set one by one.
- Kubernetes manifests in `deploy/kubernetes/`: ConfigMap, Deployment, two Services,
  a `kustomization.yaml`, and examples for a Secret, an Ingress and a Gateway API route.
- README: one table of all settings and where each comes from.

### Fixed

- Resuming at a position after a queue reload could still start at 0:00 now and then.
  Right after a reload the WiiM can report its old state for a moment, including the
  old position. The bridge took that for the new track, jumped too early, and then read
  the old position as proof that the jump had worked. It now waits until it has seen
  the new track near its beginning before it jumps. A position within the first three
  seconds is not jumped to at all.
- At `info` level the bridge logs one line for each resume, with the number of jumps
  it took.

### Removed

- `tools/ma_skip_baseline.py`, a measuring tool for another music server that had
  nothing to do with the bridge.

### Upgrading from 0.2

Set `BRIDGE_DEVICE_HOST` and `BRIDGE_ADVERTISE_HOST` (or `--device-host` and
`--advertise-host`). Arguments that were passed before keep working.

## [0.2.1] - 2026-10-11

### Fixed

- A queue that is loaded with a position to resume at now really resumes there. The
  bridge used to send the jump to the WiiM in the same instant it loaded the queue, and
  the device dropped it, so playback started at 0:00. The bridge now waits until the
  device plays the right track, jumps, checks that the position moved, and tries again
  if it did not. It gives up after about eight seconds and logs a warning.

  Seen in practice when a client reloads the queue mid-song, for example when shuffle
  is switched on in Feishin: the song restarted from the beginning. It now continues
  where it was.
- The same applies to a skip to another track with a position.

## [0.2.0] - 2026-10-11

### Added

- Logging with a level: `--log-level` or `BRIDGE_LOG_LEVEL`, with `debug`, `info`,
  `warn` or `error`. The default is `info`.
- New log lines: every jukebox command at `info` (status polls at `debug`), a failed
  command, a lost device queue, a failed pass-through to Navidrome and a rejected audio
  request at `warn`.
- Web addresses in logged error messages lose everything after the `?`, so no login
  value can reach the log.
- A build workflow: tests on every push, and an image for amd64 and arm64 on a version
  tag.
- README.

### Changed

- All output now goes through the logger to standard output, with time, level and
  source on each line. The startup lines and the line per fetched track are logged at
  `info`, so they no longer appear at `warn`.

## [0.1.0] - 2026-10-11

First version. Published as an image only; there is no git tag for it.

### Added

- Subsonic `jukeboxControl` endpoint that keeps the play queue inside the WiiM:
  `get`, `status`, `set`, `add`, `start`, `stop`, `skip`, `remove`, `clear`, `setGain`.
- Audio proxy that hands the original file from Navidrome to the device unchanged,
  with a path secret instead of a login.
- Pass-through of every other request to Navidrome, so a Subsonic client can use the
  bridge as its server.
- Volume mapped one to one, with an optional ceiling (`--max-volume`).
- Mute as an extension to the jukebox protocol: a `muted` field in the status and a
  `setMute` action.
- Phone remote page at `/remote/`: player, queue and library with search.
- The device queue is cleared when the bridge starts (`--keep-queue-on-start` turns
  that off).
- Container support: credentials and path secret from environment variables,
  `/healthz`, a Dockerfile.

[0.3.0]: https://github.com/m8-t/navidrome-wiim-bridge/releases/tag/v0.3.0
[0.2.1]: https://github.com/m8-t/navidrome-wiim-bridge/releases/tag/v0.2.1
[0.2.0]: https://github.com/m8-t/navidrome-wiim-bridge/releases/tag/v0.2.0
