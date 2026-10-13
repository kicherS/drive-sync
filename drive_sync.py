#!/usr/bin/env python3
"""User-configured rclone synchronization for Linux. Standard library only."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


class SyncError(Exception):
    """An actionable configuration or safety error."""


def xdg_path(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


def config_default() -> Path:
    return xdg_path("XDG_CONFIG_HOME", ".config") / "drive-sync/config.json"


def state_root() -> Path:
    return xdg_path("XDG_STATE_HOME", ".local/state") / "drive-sync"


def overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def path_value(value: object, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise SyncError(f"{label}: expected a nonempty path")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def object_keys(value: object, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise SyncError(f"{label}: expected an object")
    unknown = value.keys() - allowed
    if unknown:
        raise SyncError(f"{label}: unknown keys: {', '.join(sorted(unknown))}")
    return value


def integer(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise SyncError(f"{label}: expected an integer from {minimum} to {maximum}")
    return value


def boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise SyncError(f"{label}: expected true or false")
    return value


def unique_keys(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SyncError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    except (OSError, ValueError) as exc:
        raise SyncError(f"Cannot read {path}: {exc}") from exc


def load_config(path: Path) -> dict:
    data = object_keys(read_json(path), {"version", "jobs", "run_timeout", "io_timeout"}, "config")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise SyncError("config.version must be 1")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise SyncError(f"Add at least one job to {path}; see examples/config.json")
    config = {
        "path": path,
        "run_timeout": integer(data.get("run_timeout", 21600), 1, 86400, "run_timeout"),
        "io_timeout": integer(data.get("io_timeout", 300), 1, 3600, "io_timeout"),
        "jobs": [],
    }
    protected = [path, xdg_path("XDG_CONFIG_HOME", ".config"),
                 state_root().resolve(), Path.home() / ".ssh"]
    ids = set()
    for raw in jobs:
        item = object_keys(raw, {
            "id", "mode", "remote", "local", "filter_file", "skip_google_docs",
            "max_delete_percent", "max_delete_files", "check_access",
        }, "job")
        job_id = item.get("id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", job_id):
            raise SyncError("job.id must use 1-64 letters, digits, underscores or hyphens")
        if job_id in ids:
            raise SyncError(f"Duplicate job id: {job_id}")
        ids.add(job_id)
        mode = item.get("mode")
        if mode not in ("bisync", "download"):
            raise SyncError(f"{job_id}: mode must be bisync or download")
        remote = item.get("remote")
        if not isinstance(remote, str) or not re.fullmatch(r"[\w][\w .-]*:[^\r\n\x00]*", remote):
            raise SyncError(f"{job_id}: remote must be a configured rclone remote, e.g. mydrive:Documents")
        remote_name, remote_path = remote.split(":", 1)
        if ".." in PurePosixPath(remote_path).parts or "\\" in remote_path:
            raise SyncError(f"{job_id}: remote paths cannot contain '..' or backslashes")
        local = path_value(item.get("local"), path.parent, f"{job_id}.local")
        if local == Path.home().resolve() or any(overlap(local, folder.resolve()) for folder in protected):
            raise SyncError(f"{job_id}: local path overlaps a home, configuration, state or key directory")
        # rclone interprets a colon before the first slash as a remote; local is absolute.
        filter_file = None
        filter_text = None
        if "filter_file" in item:
            filter_file = path_value(item["filter_file"], path.parent, f"{job_id}.filter_file")
            try:
                filter_text = filter_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise SyncError(f"{job_id}: cannot read filter file: {exc}") from exc
            # Indirection would make baseline validation dependent on unseen files.
            if any(line.lstrip().startswith(":") for line in filter_text.splitlines()):
                raise SyncError(f"{job_id}: filter file must contain inline rules only")
        job = {
            "id": job_id, "mode": mode, "remote": remote, "local": local,
            "remote_name": remote_name,
            "remote_path": PurePosixPath("/" + remote_path.lstrip("/")),
            "filter_file": filter_file, "filter_text": filter_text,
            "skip_google_docs": boolean(item.get("skip_google_docs", True), f"{job_id}.skip_google_docs"),
            "check_access": boolean(item.get("check_access", False), f"{job_id}.check_access"),
            "max_delete_percent": integer(item.get("max_delete_percent", 25), 0, 100, f"{job_id}.max_delete_percent"),
            "max_delete_files": integer(item.get("max_delete_files", 50), 0, 1000000, f"{job_id}.max_delete_files"),
        }
        if mode == "download" and ("check_access" in item or "max_delete_percent" in item):
            raise SyncError(f"{job_id}: check_access and max_delete_percent are for bisync jobs")
        if mode == "bisync" and "max_delete_files" in item:
            raise SyncError(f"{job_id}: max_delete_files is for download jobs")
        for other in config["jobs"]:
            if overlap(local, other["local"]):
                raise SyncError(f"Overlapping local paths: {other['id']} and {job_id}")
            if remote_name == other["remote_name"] and overlap(job["remote_path"], other["remote_path"]):
                raise SyncError(f"Overlapping remote paths: {other['id']} and {job_id}")
        config["jobs"].append(job)
    return config


def job_directory(config: dict, job: dict) -> Path:
    config_id = hashlib.sha256(str(config["path"]).encode()).hexdigest()[:16]
    identity = {key: str(job[key]) for key in (
        "remote", "local", "mode", "filter_text", "skip_google_docs", "check_access",
    )}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return state_root() / config_id / job["id"] / fingerprint


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def check_dependencies(config: dict) -> None:
    if not shutil.which("rclone"):
        raise SyncError("rclone is required; install rclone 1.71 or newer")
    try:
        version = subprocess.run(["rclone", "version"], capture_output=True, text=True, check=True, timeout=15,
                                 env=rclone_environment())
        match = re.search(r"rclone v(\d+)\.(\d+)", version.stdout)
        if not match or tuple(map(int, match.groups())) < (1, 71):
            raise SyncError("rclone 1.71 or newer is required")
        remotes = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, check=True, timeout=15,
                                 env=rclone_environment())
    except (subprocess.SubprocessError, OSError) as exc:
        raise SyncError(f"Cannot query rclone: {exc}") from exc
    available = set(remotes.stdout.splitlines())
    for job in config["jobs"]:
        if job["remote_name"] + ":" not in available:
            raise SyncError(f"{job['id']}: remote is missing; configure it with 'rclone config'")
        if not job["local"].is_dir():
            raise SyncError(f"{job['id']}: create the local directory first: {job['local']}")


def rclone_environment() -> dict[str, str]:
    # Global rclone environment switches can override nested bisync operations,
    # even when their top-level CLI flag is explicitly false. Keep credentials and
    # named-remote configuration, but take transfer policy only from this wrapper.
    return {key: value for key, value in os.environ.items()
            if not key.startswith("RCLONE_") or key.startswith("RCLONE_CONFIG_")
            or key in ("RCLONE_CONFIG", "RCLONE_PASSWORD_COMMAND")}


@contextlib.contextmanager
def run_lock():
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "run.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("Another Drive Sync process is running") from exc
        yield


def command(config: dict, job: dict, workdir: Path, args: argparse.Namespace) -> list[str]:
    common = ["--verbose", "--stats", "30s", "--timeout", f"{config['io_timeout']}s", "--retries", "1",
              "--dry-run=" + str(args.dry_run).lower(), "--ignore-errors=false", "--delete-excluded=false",
              "--drive-skip-gdocs=" + str(job["skip_google_docs"]).lower()]
    if job["mode"] == "bisync":
        result = ["rclone", "bisync", str(job["local"]), job["remote"],
                  "--workdir", str(workdir), "--resilient", "--recover", "--force=false", "--max-lock", "0s",
                  "--conflict-resolve", "none", "--conflict-loser", "num",
                  "--max-delete", str(job["max_delete_percent"]), "--create-empty-src-dirs"]
        if args.resync:
            result += ["--resync", "--resync-mode", args.resync_mode]
        else:
            result += ["--resync=false", "--resync-mode", "none"]
        if job["check_access"]:
            result.append("--check-access")
        if job["filter_file"]:
            result += ["--filters-file", str(workdir.parent / "filters.txt")]
    else:
        result = ["rclone", "sync", job["remote"], str(job["local"]),
                  "--max-delete", str(job["max_delete_files"]), "--delete-after", "--create-empty-src-dirs"]
        if job["filter_file"]:
            result += ["--filter-from", str(workdir.parent / "filters.txt")]
    return result + common


class Runner:
    """Own the child process and retain interruption state until verified success."""

    def __init__(self):
        self.child = None
        self.interrupted = False

    def stop(self, _signum=None, _frame=None):
        self.interrupted = True

    def execute(self, argv: list[str], log: Path, timeout: int) -> int:
        if self.interrupted:
            return 130
        with log.open("a", encoding="utf-8") as output:
            self.child = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                          env=rclone_environment())
            started = time.monotonic()
            stopping = None
            timed_out = False
            try:
                while self.child.poll() is None:
                    if not self.interrupted and time.monotonic() - started >= timeout:
                        timed_out = True
                        self.stop()
                    if self.interrupted:
                        if stopping is None:
                            stopping = time.monotonic()
                            try:
                                os.killpg(self.child.pid, signal.SIGINT)
                            except ProcessLookupError:
                                pass
                        elif time.monotonic() - stopping >= 100:
                            os.killpg(self.child.pid, signal.SIGKILL)
                            break
                    time.sleep(0.1)
                code = self.child.wait()
                return 124 if timed_out else 130 if self.interrupted else code
            finally:
                if self.child.poll() is None:
                    os.killpg(self.child.pid, signal.SIGKILL)
                    self.child.wait()
                self.child = None


def rotate_log(path: Path) -> None:
    if path.exists() and path.stat().st_size >= 5 * 1024 * 1024:
        path.replace(path.with_suffix(".log.1"))


def run_job(config: dict, job: dict, args: argparse.Namespace, runner: Runner) -> int:
    directory = job_directory(config, job)
    initialized = (directory / "initialized").exists()
    pending = directory / "pending.json"
    if job["mode"] == "bisync" and not args.resync and not initialized:
        raise SyncError(f"{job['id']}: initialization required; preview --only {job['id']} --resync --dry-run")
    if pending.exists() and not (args.recover or args.resync):
        raise SyncError(f"{job['id']}: previous run did not finish; inspect logs and preview --only {job['id']} --recover --dry-run")
    if args.resync and job["mode"] != "bisync":
        raise SyncError("--resync is only valid for a bisync job")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = directory / ("preview.log" if args.dry_run else "rclone.log")
    rotate_log(log)
    logging.info("%s: %s%s; log: %s", job["id"], job["mode"], " (preview)" if args.dry_run else "", log)
    with contextlib.ExitStack() as stack:
        if args.dry_run:
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="drive-sync-preview-")))
            workdir = scratch / "bisync"
            if (directory / "bisync").exists():
                shutil.copytree(directory / "bisync", workdir)
            # rclone stores the filters checksum beside the filters file, outside workdir.
            if (directory / "filters.txt.md5").exists():
                shutil.copyfile(directory / "filters.txt.md5", scratch / "filters.txt.md5")
        else:
            workdir = directory / "bisync"
            atomic_json(pending, {"pid": os.getpid(), "started": time.time()})
        workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if job["filter_file"]:
            (workdir.parent / "filters.txt").write_text(job["filter_text"], encoding="utf-8")
        code = runner.execute(command(config, job, workdir, args), log, config["run_timeout"])
    if not args.dry_run:
        atomic_json(directory / "result.json", {"finished": time.time(), "exit_code": code})
        if code == 0:
            if job["mode"] == "bisync":
                (directory / "initialized").touch(mode=0o600)
            pending.unlink(missing_ok=True)
    if code:
        logging.error("%s: rclone failed (exit %s); inspect %s", job["id"], code, log)
    else:
        logging.info("%s: %s completed", job["id"], "preview" if args.dry_run else "synchronization")
    return code


def show_status(config: dict) -> None:
    for job in config["jobs"]:
        directory = job_directory(config, job)
        if (directory / "pending.json").exists():
            status = "running or recovery required"
        elif job["mode"] == "bisync" and not (directory / "initialized").exists():
            status = "initialization required"
        elif (directory / "result.json").exists():
            result = read_json(directory / "result.json")
            status = f"last exit code: {result.get('exit_code', 'unknown')}"
        else:
            status = "not run yet"
        print(f"{job['id']} [{job['mode']}]: {status}\n  State: {directory}")


def desktop_message(message: str, error: bool = False) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        if shutil.which("zenity"):
            subprocess.run(["zenity", "--error" if error else "--info", "--no-markup", "--title=Drive Sync", "--text", message], check=False)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "--app-name=Drive Sync", "Drive Sync", message], check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass  # Optional desktop integration must not turn success into failure.


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synchronize configured folders with rclone.")
    parser.add_argument("--config", type=Path, default=config_default(), help="JSON configuration path")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init", action="store_true", help="create an empty configuration; never overwrite")
    action.add_argument("--check", action="store_true", help="validate configuration and local prerequisites")
    action.add_argument("--status", "--stato", action="store_true", help="read status without creating files")
    action.add_argument("--resync", action="store_true", help="explicitly initialize or rebuild one bisync baseline")
    action.add_argument("--recover", action="store_true", help="explicitly retry one failed or interrupted job")
    parser.add_argument("--resync-mode", choices=("newer", "path1", "path2"), help="resync winner: newer, local (path1) or remote (path2)")
    parser.add_argument("--dry-run", action="store_true", help="preview in disposable bisync state")
    parser.add_argument("--only", "--solo", help="run only this job id")
    parser.add_argument("--manual", action="store_true", help="confirm and display results on graphical desktops")
    args = parser.parse_args(argv)
    if (args.resync or args.recover) and not args.only:
        parser.error("--resync and --recover require --only JOB")
    if args.resync_mode and not args.resync:
        parser.error("--resync-mode requires --resync")
    if (args.init or args.check or args.status) and (args.dry_run or args.manual):
        parser.error("--init, --check and --status cannot be combined with --dry-run or --manual")
    if args.init and args.only:
        parser.error("--init cannot be combined with --only")
    args.resync_mode = args.resync_mode or "newer"
    args.config = args.config.expanduser().resolve()
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.init:
            args.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with args.config.open("x", encoding="utf-8") as handle:
                json.dump({"version": 1, "jobs": []}, handle, indent=2)
                handle.write("\n")
            print(f"Created {args.config}; add jobs before running Drive Sync.")
            return 0
        if not args.config.is_file():
            raise SyncError(f"Configuration not found: {args.config}. Run drive-sync --init, then add jobs using the README.")
        config = load_config(args.config)
        if args.only:
            config["jobs"] = [job for job in config["jobs"] if job["id"] == args.only]
            if not config["jobs"]:
                raise SyncError(f"Unknown job id: {args.only}")
        if args.status:
            show_status(config)
            return 0
        check_dependencies(config)
        if args.check:
            print("Configuration, local directories and rclone remotes are valid. Remote connectivity was not tested.")
            return 0
        if args.manual and shutil.which("zenity") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            message = "Run Drive Sync now?\nJobs: " + ", ".join(job["id"] for job in config["jobs"])
            if not args.dry_run:
                message += "\nConfigured synchronization can overwrite and delete files."
            if subprocess.run(["zenity", "--question", "--no-markup", "--title=Drive Sync", "--text", message], check=False).returncode:
                return 0
        runner = Runner()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, runner.stop)
        failed = False
        with run_lock():
            handler = RotatingFileHandler(state_root() / "sync.log", maxBytes=1024 * 1024, backupCount=2)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(handler)
            try:
                for job in config["jobs"]:
                    if runner.interrupted:
                        break
                    try:
                        failed = bool(run_job(config, job, args, runner)) or failed
                    except (SyncError, OSError) as exc:
                        logging.error("%s", exc)
                        failed = True
            finally:
                logging.getLogger().removeHandler(handler)
                handler.close()
        code = 130 if runner.interrupted else 1 if failed else 0
        if args.manual:
            desktop_message("Synchronization failed or was interrupted. Run drive-sync --status and inspect the logs."
                            if code else "Preview completed." if args.dry_run else "Synchronization completed.", error=bool(code))
        return code
    except (SyncError, OSError) as exc:
        logging.error("%s", exc)
        if args.manual:
            desktop_message(str(exc), error=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
