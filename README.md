# Drive Sync

Synchronize folders between Linux and Google Drive with [rclone](https://rclone.org/).
Drive Sync adds per-user configuration, a desktop launcher, optional scheduling,
logs and explicit recovery to rclone's synchronization commands.

## How it works

Each configured **job** connects one local directory to one rclone remote path:

| Mode | Direction | Behavior |
| --- | --- | --- |
| `bisync` | Local ↔ remote | Propagates additions, changes and deletions on either side. Requires an explicit first initialization. |
| `download` | Remote → local | Makes the local directory match the remote. Local-only files may be deleted; local edits may be overwritten. Never uploads local edits. |

Synchronization is **not a backup**. Keep an independent backup of important files,
inspect a dry run before the first real run, and avoid editing the same files on
both sides while synchronization is running. A deletion limit can stop a run
partway through; it does not make the operation atomic.

There are no bundled accounts, active folder mappings or automatic exclusions.
Authentication belongs to rclone and stays outside the package. Google Docs,
Sheets and Slides are skipped by default; see [configuration](docs/configuration.md).

## Requirements

- Linux, Bash, Python **3.10+** and rclone **1.71+**.
- An rclone remote configured for your account: run `rclone config`.
- Optional: systemd user services for scheduling.
- Optional: Zenity for desktop confirmation and result dialogs, or `notify-send`
  for result notifications. Command-line operation also works without a desktop.

Use the [rclone installation instructions](https://rclone.org/install/) if your
Linux distribution provides an older version. Drive Sync calls the `rclone`
executable on your `PATH`; it does not bundle or update it.

## Install

Run these commands as your regular user, without `sudo`, from the source directory:

```bash
./install.sh
```

On a system without a systemd user session:

```bash
./install.sh --no-systemd
```

The installer places `drive-sync` in `~/.local/bin`, adds the **Drive Sync**
application launcher, and installs optional systemd units. Ensure `~/.local/bin`
is on your `PATH`. XDG configuration and data directory overrides are respected.
It does not create sync folders, copy credentials or enable the timer.
An upgrade stops and disables an existing `drive-sync` timer before replacing files.

To run from a source checkout without installing:

```bash
./gdrive-sync.sh --help
```

## Configure and run

### 1. Choose a remote and folder

Create a remote with `rclone config`, or list existing names with `rclone listremotes`.
In this example, `mydrive:` is a placeholder for **your** remote, and `Documents`
is an existing folder on that remote.

```bash
drive-sync --init
mkdir -p ~/GoogleDrive/Documents
```

Edit `${XDG_CONFIG_HOME:-$HOME/.config}/drive-sync/config.json`:

```json
{
  "version": 1,
  "jobs": [
    {
      "id": "documents",
      "mode": "bisync",
      "remote": "mydrive:Documents",
      "local": "~/GoogleDrive/Documents"
    }
  ]
}
```

Use dedicated local folders. Do not point multiple jobs at overlapping trees.
Configuration is JSON data; it is never executed as shell code.

### 2. Validate and preview initialization

```bash
drive-sync --check
drive-sync --only documents --resync --dry-run
```

`--check` validates configuration, local directories, the rclone version and
configured remote names. It does not contact Google Drive or test authentication.
The preview contacts the remote; inspect the `preview.log` path printed by the command.

### 3. Initialize once, then synchronize normally

When the preview is correct:

```bash
drive-sync --only documents --resync
drive-sync --dry-run
drive-sync
```

**Initialization merges the two file sets.** With the default `--resync-mode newer`,
a newer file can overwrite the other version. Files present on only one side are
copied to the other side, so previously deleted files can reappear. Make a backup
before initialization or recovery. `--resync` is not a routine synchronization option.

After initialization, normal bisync conflicts preserve both versions using numbered
conflict filenames. Review those files and resolve the conflict yourself.
See the [rclone bisync manual](https://rclone.org/bisync/) for its semantics and limitations.

A `download` job needs no bisync initialization: preview with `--only JOB --dry-run`,
then run with `--only JOB`. Its local directory is a disposable mirror, not a place
for independent edits. See [examples/config.json](examples/config.json) for both modes.

## Filters

Filters are optional and belong to the user's configuration. Copy
[examples/filters.txt](examples/filters.txt) into your configuration directory,
review its rules, then add `"filter_file": "filters.txt"` to a job.
Without this setting, Drive Sync supplies **no file exclusions**.

Changing a bisync filter or endpoint requires a fresh, explicit initialization.
The command refuses to reuse an incompatible baseline. Excluded files already on
either side are left in place; a filter is not a cleanup command.
See [configuration and filter details](docs/configuration.md).

## Desktop and automatic runs

Open **Drive Sync** from your applications menu after configuring and initializing
all bisync jobs. With Zenity installed, a dialog asks before starting and shows the
result. Detailed progress and errors are written to the logs.

To schedule runs every 30 minutes, enable the installed timer **after a successful
manual run**:

```bash
systemctl --user enable --now drive-sync.timer
systemctl --user list-timers drive-sync.timer
```

The timer can run shortly after it is enabled, including to catch up on a missed
run. It uses the default configuration path. To use a different file, override
`ExecStart` with `systemctl --user edit drive-sync.service` and pass `--config /absolute/path/config.json`.
Only the environment available to the user service manager is used; a shell-only
XDG override or rclone credential variable is not automatically inherited.

Stop automatic runs and an active scheduled run:

```bash
systemctl --user disable --now drive-sync.timer
systemctl --user stop drive-sync.service
```

The service requests a graceful stop and allows up to 120 seconds for cleanup.
The command handles Ctrl+C, termination and timeouts. An incomplete job is held for
explicit recovery. Scheduling while logged out depends on your distribution's
user service settings; the installer does not change login or power policy.

## Status, logs and recovery

```bash
drive-sync --status
drive-sync --only documents --dry-run
```

Status is read-only. Runtime files are stored under
`${XDG_STATE_HOME:-$HOME/.local/state}/drive-sync/`, with separate baselines for each
configuration and job definition. The printed job state directory contains:

- `rclone.log` and `preview.log`: rclone output;
- `bisync/`: the working baseline;
- `pending.json`: an unfinished or failed run;
- `result.json`: the last actual run's exit code and completion time.

Logs can contain filenames and directory paths. Keep them private and redact them
before reporting an issue. Summary logs rotate at 1 MiB; job logs rotate before the
next run once they reach 5 MiB. A long run can exceed that size.

A dry run uses a disposable copy of the bisync baseline. It writes a preview log
but does not initialize a job, clear failure state or change synchronized data.
For a failed or interrupted job, follow [the recovery guide](docs/recovery.md).

## Commands

| Option | Purpose |
| --- | --- |
| `--config FILE` | Select a configuration file. |
| `--init` | Create an empty configuration without overwriting an existing file. |
| `--check` | Check configuration and local prerequisites. |
| `--status` | Read job state without creating files. |
| `--only JOB` | Select one job by its ID. |
| `--dry-run` | Preview changes using disposable bisync state. |
| `--resync --only JOB` | Explicitly initialize or rebuild one bisync job. |
| `--resync-mode newer\|path1\|path2` | Choose the winning version during resync: newer, local or remote. |
| `--recover --only JOB` | Retry one failed job after inspecting the cause. |
| `--manual` | Use optional desktop confirmation and result dialogs. |

Exit codes: `0` success (or a cancelled desktop confirmation), `1` job failure or
required initialization/recovery, `2` invalid arguments/configuration or unavailable
prerequisites, `130` interruption or run timeout. A failed rclone command never counts
as a successful synchronization. The original rclone exit code is in `result.json`.

## Upgrade from 1.0

This configuration format replaces the original preconfigured installation.
Old filters, state and folder mappings are not imported. Stop the previous timer
and service before installing; do not run old and new installations against the
same data. Recreate your jobs explicitly, preserve a backup and initialize each
bisync job after reviewing its preview. The new command and units are named
`drive-sync`; remove an older installation using its matching uninstaller only
after checking which files it owns.

## Uninstall

```bash
./uninstall.sh
# If installed without systemd:
./uninstall.sh --no-systemd
```

This removes package files and stops its installed services. Your configuration,
synchronized folders, logs and baselines remain. Use the same `--prefix` option
when removing a custom-prefix installation.

## Development

```bash
bash -n gdrive-sync.sh install.sh uninstall.sh
python3 -m unittest discover -s tests -v
```

The tests use temporary directories and a local rclone backend, without cloud
credentials or network access to a storage provider. Integration tests require
rclone 1.71 or newer. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Drive Sync is distributed under the [MIT license](LICENSE).
rclone is a separate dependency and is not bundled with this project.
