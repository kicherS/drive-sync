# Contributing

Keep changes portable and configuration-driven. Examples must use placeholder
remote names and generic folders; no account settings, credentials or private
project filters belong in the package.

Use Python's standard library for the wrapper and rclone for data transfers.
Changes to sync direction, deletion behavior, baseline handling or interruption
recovery need tests against temporary local directories.

Run:

```bash
bash -n gdrive-sync.sh install.sh uninstall.sh
python3 -m unittest discover -s tests -v
```

Tests must never use the developer's rclone configuration or synchronized folders.
Integration tests use a temporary rclone configuration with a local backend.
CI runs the same tests on the minimum supported rclone version.

Submit changes through a pull request. The required `Package validation` check
and maintainer review must pass before merge. Releases are prepared from reviewed
changes and signed tags.
