# Recovering a failed or interrupted job

Do not delete the baseline or repeatedly initialize to make an error disappear.
Inspect the cause and preserve a backup first. Failures can occur after some files
have already changed.

1. Stop the scheduled timer and any active service:

   ```bash
   systemctl --user disable --now drive-sync.timer
   systemctl --user stop drive-sync.service
   ```

   For a terminal run, request a graceful stop with Ctrl+C and wait for it to exit.

2. Read `drive-sync --status`. Inspect `rclone.log` in the reported job state
   directory and check authentication, network access, storage availability,
   permissions, quota and deletion limits as appropriate. Check that both endpoints
   still contain the expected data.

3. Preview an explicit retry of that job:

   ```bash
   drive-sync --only documents --recover --dry-run
   ```

   Recovery uses the existing baseline and rclone's recovery mechanism. A preview
   leaves the unfinished-run marker in place. When the preview is correct:

   ```bash
   drive-sync --only documents --recover
   ```

4. If rclone reports a missing or unusable bisync baseline, back up both endpoints,
   inspect the differences, and preview rebuilding it:

   ```bash
   drive-sync --only documents --resync --resync-mode newer --dry-run
   ```

   `newer` prefers the newer modification time. `path1` prefers the local version;
   `path2` prefers the remote version. These choices apply to conflicting files;
   **all modes merge files that exist on only one side**. Even `path2` can upload
   local-only files. Resync is not a “restore everything from Drive” operation,
   and it can overwrite the losing version of a file.

   After reviewing the preview and backup, repeat the selected command without
   `--dry-run`. There is no automatic resync and no option to bypass a failed state
   by discarding the marker.

5. Run a normal preview and synchronization, then re-enable the timer if desired.

## Locks

Only one Drive Sync process per user state directory can run at once, including
previews. This application lock is released by the operating system when the
process exits. Do not delete the lock file while another instance is running.

rclone also has bisync lock files in each job's `bisync/` directory. After a crash,
a stale rclone lock may remain. The wrapper does not expire or delete these locks
automatically. Follow [rclone's lock diagnosis](https://rclone.org/bisync/#lock-file):
verify that the recorded process and every other process using the same data have
stopped before removing the specific stale lock. Keep listings and other state.

## What remains blocked

A failed job stays blocked on subsequent ordinary runs. Other independent jobs
may still run; the overall command returns a failure if any selected job failed.
After a signal or whole-run timeout, no further jobs are started in that invocation.
Only a successful actual retry clears that job's unfinished-run marker.

## Reporting a problem

Include the Drive Sync revision, rclone version, Linux distribution, command,
exit code and a redacted log excerpt. Replace account names, filenames and paths
with placeholders. Never include `rclone.conf`, OAuth tokens, private keys or
unredacted user configuration in a public issue.
