# Changelog

## 2.0.0 - 2026-10-13

First public release, distributed under the MIT license.

### Added

- Validated per-user JSON configuration for bidirectional and download-only jobs.
- Generic optional filter examples and a configuration/recovery guide.
- Portable installation, XDG directory support and an optional desktop launcher.
- Offline tests for synchronization, filters, conflicts, failure handling and packaging.

### Changed

- Replaced the preconfigured shell orchestration with a Python standard-library
  wrapper around rclone; `gdrive-sync.sh` remains a source-checkout entry point.
- Renamed the installed command and systemd units to `drive-sync`.
- Installation leaves scheduling disabled and does not create synchronization jobs.
- Bisync initialization and recovery require an explicit job selection.
- Ordinary bisync conflicts retain both versions. Deletion limits apply to both modes.
- Dry runs use disposable baseline copies; status queries are read-only.
- Requires Python 3.10+ and rclone 1.71+.

### Removed

- Fixed user paths, account mappings, project-specific filters and installation history.
- Automatic first-run resync and recovery options that discarded interruption state.

### Migration

Existing installations need an explicit configuration migration and baseline
initialization. See the README before upgrading.

## 1.0.0

Initial Linux synchronization package with rclone, a desktop launcher and a
systemd user timer.
