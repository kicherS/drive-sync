# Configuration reference

The default file is `${XDG_CONFIG_HOME:-$HOME/.config}/drive-sync/config.json`.
Use `--config FILE` to select another file. Unknown and duplicate keys are rejected.
Relative paths are resolved against the configuration file's directory; `~`
expands to the current user's home. Shell variables such as `$HOME` are not expanded
inside JSON. Paths and filenames can contain spaces and Unicode.

## Top-level fields

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `version` | Yes | — | Configuration schema version; currently `1`. |
| `jobs` | Yes | — | Nonempty list of job objects. |
| `run_timeout` | No | `21600` | Maximum seconds per rclone run, from 1 to 86400, plus graceful shutdown time. |
| `io_timeout` | No | `300` | rclone I/O idle timeout in seconds, from 1 to 3600. This is not a whole-job stall detector. |

## Job fields

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `id` | Yes | — | Unique 1–64 character name using letters, digits, `_` and `-`; starts with a letter or digit. |
| `mode` | Yes | — | `bisync` or `download`. |
| `remote` | Yes | — | Configured rclone remote and optional path, such as `mydrive:Documents`. |
| `local` | Yes | — | Existing, dedicated local directory. Never the whole home, a configuration directory or application state. |
| `filter_file` | No | None | Path to an optional UTF-8 rclone filter file. |
| `skip_google_docs` | No | `true` | Skip Google-native documents. Set `false` only after reviewing rclone's export behavior. |
| `max_delete_percent` | Bisync only | `25` | Maximum percentage of deletions accepted by bisync, from 0 to 100. |
| `check_access` | Bisync only | `false` | Enable rclone's check for matching `RCLONE_TEST` marker files on both sides. |
| `max_delete_files` | Download only | `50` | Maximum number of local files rclone may delete, from 0 to 1000000. |

Deletion limits are guards, not backups or transactions. `0` refuses deletions.
Limits do not prevent overwrites and do not apply to the copying performed during
bisync initialization in the same way as an ordinary sync.

Before enabling `check_access`, create matching `RCLONE_TEST` files on both sides
and ensure your filters include them. This is useful for removable or mounted
storage. See [rclone access checks](https://rclone.org/bisync/#check-access).
A missing local directory is always an error; Drive Sync never recreates it silently.

## Multiple jobs

Use a separate, non-overlapping local directory for each job. Remote paths on the
same configured remote must not overlap either. Avoid using aliases or different
remote names for the same physical storage: their equivalence cannot be inferred
from the configuration. Do not synchronize the same files from another configuration,
program or computer at the same time.

Changing the configuration file's location, job ID, endpoint, filter contents or
file-selection options creates a different baseline identity. A bisync job then
requires an explicit `--resync`. Old state remains available for diagnosis.
Changing only timeouts or deletion limits does not reset a baseline.

## Filters

Drive Sync does not exclude project names, dotfiles, developer tools or logs by
default. Choose your own exclusions for credentials, temporary files and content
you do not want uploaded.

For example, with `"filter_file": "filters.txt"`:

```text
# Skip temporary editor files.
- *.swp
- *~

# Optional: skip version-control metadata.
- .git/**
```

The file uses [rclone filtering syntax](https://rclone.org/filtering/).
Only inline rules are supported; nested filter-file directives are rejected so
that changes can be detected reliably. Rules are snapshotted for each run.
For bisync, edit filters only when the job is stopped, then review
`--only JOB --resync --dry-run` before rebuilding the baseline. Files excluded by
a new rule are not automatically removed from the local or remote folder.

## Google Drive setup

Run `rclone config` and follow [rclone's Google Drive guide](https://rclone.org/drive/).
Choose your own remote name; the examples use `mydrive:` only as a placeholder.
Shared Drives and “Shared with me” access are configured in rclone. Give them
separate jobs and local directories as appropriate, without overlapping another job.

Google Docs, Sheets and Slides are cloud-native documents. Their exports are not
ordinary editable round trips. They are skipped by default. Download-only jobs
may set `skip_google_docs` to `false` to use the export formats configured in rclone.
Check the resulting filenames and formats in a preview before enabling this.

Drive Sync uses rclone's authentication and configuration lookup. It forwards
`RCLONE_CONFIG`, `RCLONE_CONFIG_*` (including `RCLONE_CONFIG_PASS` and named-remote
settings), and `RCLONE_PASSWORD_COMMAND`. Other `RCLONE_*` environment switches
are not forwarded: transfer policy and filters come from the job configuration.
Put backend options in your rclone configuration or named-remote environment
settings, not global environment flags. Other provider credential variables,
such as AWS credential variables, are inherited normally.

Never put credentials or tokens in this JSON file or in the repository. An
encrypted rclone configuration must be unlocked through an appropriate rclone
mechanism for unattended runs. Changes to an account or root folder inside the
rclone configuration cannot be detected by Drive Sync: after such changes, back
up the data and explicitly initialize a new job instead of reusing an old baseline.

## Installed paths

| Component | Default location |
| --- | --- |
| Executable | `~/.local/bin/drive-sync` |
| User configuration | `$XDG_CONFIG_HOME/drive-sync/config.json` |
| Service and timer | `$XDG_CONFIG_HOME/systemd/user/drive-sync.*` |
| Launcher | `$XDG_DATA_HOME/applications/drive-sync.desktop` |
| Icon | `$XDG_DATA_HOME/icons/hicolor/scalable/apps/drive-sync.svg` |
| Example files | `$XDG_DATA_HOME/drive-sync/examples/` |
| Runtime state | `$XDG_STATE_HOME/drive-sync/` |

Unset or relative XDG directory variables fall back to the standard directories
under the current user's home. Config files created by `--init`, runtime state
and logs are private to the current user. Protect configuration files you create
manually as well.

For packaging or a temporary installation, `./install.sh --prefix /absolute/path`
puts files in `bin/`, `share/` and `config/` under that prefix and never contacts
the live service manager. The prefix does not change runtime configuration lookup
or activate the installed units. Use an absolute prefix path that will remain valid.
Explicit prefix packaging is also supported in containers running as root. Normal
per-user installation and uninstallation still require an unprivileged user.
