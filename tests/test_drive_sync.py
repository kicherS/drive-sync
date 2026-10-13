import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("drive_sync", ROOT / "drive_sync.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="drive-sync tests é ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.path = self.config_dir / "config.json"
        self.local = self.root / "local documents"
        self.remote = self.root / "remote documents"
        self.local.mkdir()
        self.remote.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}
        self.env.update({
            "XDG_CONFIG_HOME": str(self.config_dir),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "RCLONE_CONFIG": str(self.root / "rclone.conf"),
        })
        (self.root / "rclone.conf").write_text("[fixture]\ntype = local\n")
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.job = {"id": "documents", "mode": "bisync", "remote": f"fixture:{self.remote}", "local": str(self.local)}
        self.save()

    def save(self, **overrides):
        self.path.write_text(json.dumps({"version": 1, "jobs": [self.job], **overrides}), encoding="utf-8")

    def cli(self, *arguments, expect=0):
        result = subprocess.run([sys.executable, str(ROOT / "drive_sync.py"), "--config", str(self.path), *arguments],
                                env=os.environ.copy(), capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def job_dir(self):
        config = app.load_config(self.path)
        return app.job_directory(config, config["jobs"][0])

    def fake_rclone(self, code=0, sleep=False):
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        script = binary / "rclone"
        script.write_text(
            "#!" + sys.executable + "\nimport sys, time, signal\nfrom pathlib import Path\n"
            "if sys.argv[1] == 'version':\n print('rclone v1.71.0'); sys.exit(0)\n"
            "if sys.argv[1] == 'listremotes':\n print('fixture:'); sys.exit(0)\n"
            f"Path({str(self.root / 'called')!r}).write_text('called')\n"
            + ("signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\ntime.sleep(300)\n" if sleep else "")
            + f"sys.exit({code})\n"
        )
        script.chmod(0o755)
        os.environ["PATH"] = str(binary) + os.pathsep + self.env["PATH"]


class ConfigurationTests(Workspace):
    def test_status_is_read_only_without_rclone(self):
        os.environ["PATH"] = ""
        self.cli("--status")
        self.assertFalse((self.root / "state").exists())

    def test_init_does_not_overwrite(self):
        before = self.path.read_bytes()
        self.cli("--init", expect=2)
        self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        self.cli("--init")
        self.assertEqual(json.loads(self.path.read_text()), {"version": 1, "jobs": []})
        self.cli(expect=2)

    def test_bad_arguments_never_invoke_rclone(self):
        self.fake_rclone()
        for arguments in (("--only",), ("--only", "missing"), ("--resync",), ("--recover",),
                          ("--recovery-mode", "forza"), ("--status", "--dry-run"), ("--unexpected",)):
            with self.subTest(arguments=arguments):
                self.cli(*arguments, expect=2)
        self.assertFalse((self.root / "called").exists())

    def test_reject_unknown_and_duplicate_keys(self):
        self.save(typo=True)
        with self.assertRaises(app.SyncError):
            app.load_config(self.path)
        self.path.write_text('{"version": 1, "version": 1, "jobs": []}')
        with self.assertRaises(app.SyncError):
            app.load_config(self.path)

    def test_reject_bad_jobs(self):
        original = self.job.copy()
        for values in ({"id": "../escape"}, {"mode": "upload"}, {"remote": "--bad:"},
                       {"remote": "fixture:path/../other"}, {"local": str(Path.home())},
                       {"local": str(self.config_dir)}, {"local": "/"},
                       {"filter_file": "missing"}, {"skip_google_docs": "false"},
                       {"max_delete_percent": True}, {"max_delete_files": 5}):
            with self.subTest(values=values):
                self.job = {**original, **values}
                self.save()
                with self.assertRaises(app.SyncError):
                    app.load_config(self.path)

    def test_reject_overlapping_local_and_remote_paths(self):
        other = {**self.job, "id": "another", "remote": "fixture:other"}
        self.save(jobs=[self.job, other])
        with self.assertRaisesRegex(app.SyncError, "Overlapping local"):
            app.load_config(self.path)
        other.update(local=str(self.root / "another"), remote=self.job["remote"] + "/child")
        self.save(jobs=[self.job, other])
        with self.assertRaisesRegex(app.SyncError, "Overlapping remote"):
            app.load_config(self.path)
        link = self.root / "link"
        link.symlink_to(self.local, target_is_directory=True)
        other.update(local=str(link), remote="fixture:other")
        self.save(jobs=[self.job, other])
        with self.assertRaisesRegex(app.SyncError, "Overlapping local"):
            app.load_config(self.path)

    def test_filter_edit_invalidates_baseline(self):
        filter_file = self.config_dir / "filters.txt"
        filter_file.write_text("- *.tmp\n")
        self.job["filter_file"] = "filters.txt"
        self.save()
        first = self.job_dir()
        filter_file.write_text("- *.bak\n")
        self.assertNotEqual(first, self.job_dir())

    def test_check_does_not_sync_or_write_state(self):
        self.fake_rclone()
        self.cli("--check")
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "called").exists())


class FailureTests(Workspace):
    def setUp(self):
        super().setUp()
        self.job["mode"] = "download"
        self.save()

    def test_nonzero_exit_blocks_retries_and_preview_does_not_clear_failure(self):
        self.fake_rclone(code=2)
        self.cli(expect=1)
        directory = self.job_dir()
        self.assertTrue((directory / "pending.json").exists())
        self.assertEqual(json.loads((directory / "result.json").read_text())["exit_code"], 2)
        (self.root / "called").unlink()
        self.fake_rclone(code=0)
        self.cli(expect=1)
        self.assertFalse((self.root / "called").exists())
        self.cli("--only", "documents", "--recover", "--dry-run")
        self.assertTrue((directory / "pending.json").exists())
        self.cli("--only", "documents", "--recover")
        self.assertFalse((directory / "pending.json").exists())

    def test_missing_directory_is_not_recreated(self):
        self.fake_rclone()
        self.local.rmdir()
        self.cli(expect=2)
        self.assertFalse(self.local.exists())
        self.assertFalse((self.root / "called").exists())

    def test_lock_contention_fails_closed(self):
        self.fake_rclone()
        with app.run_lock():
            self.cli(expect=2)
        self.assertFalse((self.root / "called").exists())

    def test_timeout_retains_interruption_marker(self):
        self.fake_rclone(sleep=True)
        self.save(run_timeout=1)
        self.cli(expect=130)
        self.assertEqual(json.loads((self.job_dir() / "result.json").read_text())["exit_code"], 124)
        self.assertTrue((self.job_dir() / "pending.json").exists())

    def test_termination_waits_for_child_and_keeps_marker(self):
        self.fake_rclone(sleep=True)
        process = subprocess.Popen([sys.executable, str(ROOT / "drive_sync.py"), "--config", str(self.path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "called").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.root / "called").exists())
            process.terminate()
            output = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, output)
            self.assertTrue((self.job_dir() / "pending.json").exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


@unittest.skipUnless(shutil.which("rclone"), "integration tests require rclone >= 1.71")
class RcloneIntegrationTests(Workspace):
    def snapshot(self, directory):
        return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}

    def initialize(self):
        (self.local / "from-local.txt").write_text("local content")
        (self.remote / "from-remote.txt").write_text("remote content")
        self.job["max_delete_percent"] = 90
        self.save()
        self.cli("--only", "documents", "--resync")

    def test_preview_initialization_and_bidirectional_changes(self):
        (self.remote / "from-remote.txt").write_text("remote content")
        (self.local / "from-local.txt").write_text("local content")
        before = (self.snapshot(self.local), self.snapshot(self.remote))
        self.cli(expect=1)
        self.cli("--only", "documents", "--resync", "--dry-run")
        self.assertEqual(before, (self.snapshot(self.local), self.snapshot(self.remote)))
        self.assertFalse((self.job_dir() / "initialized").exists())
        self.initialize()
        self.assertEqual(self.snapshot(self.local), self.snapshot(self.remote))
        # Ambient rclone switches must not silently turn a real run into a preview/resync.
        os.environ["RCLONE_DRY_RUN"] = "true"
        os.environ["RCLONE_RESYNC"] = "true"
        os.environ["RCLONE_RESYNC_MODE"] = "path2"
        (self.local / "new.txt").write_text("new")
        self.cli()
        self.assertEqual((self.remote / "new.txt").read_text(), "new")
        (self.local / "from-local.txt").unlink()
        self.cli()
        self.assertFalse((self.remote / "from-local.txt").exists())

    def test_preview_preserves_live_baseline(self):
        self.initialize()
        (self.remote / "next.txt").write_text("pending download")
        baseline = self.snapshot(self.job_dir() / "bisync")
        data = (self.snapshot(self.local), self.snapshot(self.remote))
        self.cli("--dry-run")
        self.assertEqual(baseline, self.snapshot(self.job_dir() / "bisync"))
        self.assertEqual(data, (self.snapshot(self.local), self.snapshot(self.remote)))
        self.cli()
        self.assertTrue((self.local / "next.txt").exists())

    def test_filters_apply_and_changes_require_initialization(self):
        filters = self.config_dir / "filters.txt"
        filters.write_text("- *.tmp\n")
        self.job["filter_file"] = "filters.txt"
        (self.remote / "private.tmp").write_text("excluded")
        self.initialize()
        self.assertFalse((self.local / "private.tmp").exists())
        checksum = (self.job_dir() / "filters.txt.md5").read_bytes()
        self.cli("--dry-run")
        self.assertEqual(checksum, (self.job_dir() / "filters.txt.md5").read_bytes())
        filters.write_text("- *.bak\n")
        self.cli(expect=1)
        self.assertFalse((self.local / "private.tmp").exists())

    def test_normal_conflicts_keep_both_versions(self):
        self.initialize()
        (self.local / "from-local.txt").write_text("changed on local")
        (self.remote / "from-local.txt").write_text("changed remotely and longer")
        timestamp = time.time() + 10
        os.utime(self.local / "from-local.txt", (timestamp, timestamp))
        os.utime(self.remote / "from-local.txt", (timestamp + 10, timestamp + 10))
        self.cli()
        for directory in (self.local, self.remote):
            contents = {path.read_text() for path in directory.glob("from-local.txt*")}
            self.assertIn("changed on local", contents)
            self.assertIn("changed remotely and longer", contents)

    def test_download_mirrors_only_toward_local(self):
        self.job["mode"] = "download"
        self.save()
        (self.remote / "document.txt").write_text("authoritative")
        (self.local / "local-only.txt").write_text("will be removed")
        before = self.snapshot(self.local)
        self.cli("--dry-run")
        self.assertEqual(before, self.snapshot(self.local))
        self.cli()
        self.assertEqual(self.snapshot(self.local), self.snapshot(self.remote))
        self.assertFalse((self.remote / "local-only.txt").exists())
        (self.local / "document.txt").write_text("local edits are replaced")
        self.cli()
        self.assertEqual((self.local / "document.txt").read_text(), "authoritative")

    def test_deletion_limit_is_enforced(self):
        self.job.update(mode="download", max_delete_files=0)
        self.save()
        (self.remote / "document.txt").write_text("keep")
        (self.local / "local-only.txt").write_text("guarded")
        self.cli(expect=1)
        self.assertTrue((self.local / "local-only.txt").exists())
        self.assertTrue((self.job_dir() / "pending.json").exists())


class PackageTests(Workspace):
    def test_root_packaging_requires_explicit_prefix(self):
        spec = importlib.util.spec_from_file_location("drive_sync_package", ROOT / "package.py")
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        prefix = self.root / "root-container-stage"
        with patch.object(package.os, "geteuid", return_value=0):
            with patch.object(sys, "argv", ["package.py", "install", "--prefix", str(prefix)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(package.main(), 0)
            self.assertTrue((prefix / "bin/drive-sync").is_file())
            # Keep a regression in the prohibition isolated from the real user's files.
            with patch.object(Path, "home", return_value=self.root):
                with patch.object(sys, "argv", ["package.py", "install", "--no-systemd"]):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as error:
                            package.main()
                    self.assertEqual(error.exception.code, 2)
            with patch.object(sys, "argv", ["package.py", "uninstall", "--prefix", str(prefix)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(package.main(), 0)
            self.assertFalse((prefix / "bin/drive-sync").exists())

    @unittest.skipUnless(shutil.which("gio"), "desktop launch regression requires gio")
    def test_desktop_launcher_executes_in_percent_and_quoted_paths(self):
        prefix = self.root / 'desktop $quote"percent%'
        subprocess.run([sys.executable, str(ROOT / "package.py"), "install", "--prefix", str(prefix),
                        "--no-systemd"], check=True, capture_output=True)
        executable = prefix / "bin/drive-sync"
        receipt = prefix / "launch-receipt"
        # Launch only this harmless fixture, never the actual synchronizer or a GUI.
        executable.write_text("#!" + sys.executable + "\nfrom pathlib import Path\nPath(" +
                              repr(str(receipt)) + ").write_text('launched')\n", encoding="utf-8")
        launcher = prefix / "share/applications/drive-sync.desktop"
        result = subprocess.run(["gio", "launch", str(launcher)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 5
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(receipt.exists(), "the desktop launcher did not execute the fixture")

    def test_install_and_uninstall_in_isolated_prefix(self):
        prefix = self.root / 'package space $quote"percent%'
        for action in ("install", "uninstall"):
            result = subprocess.run([sys.executable, str(ROOT / "package.py"), action, "--prefix", str(prefix)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if action == "install":
                executable = prefix / "bin/drive-sync"
                self.assertTrue(os.access(executable, os.X_OK))
                result = subprocess.run([str(executable), "--help"], capture_output=True)
                self.assertEqual(result.returncode, 0)
                service = (prefix / "config/systemd/user/drive-sync.service").read_text()
                self.assertNotIn("@EXECUTABLE@", service)
                self.assertFalse((prefix / "config/systemd/user/timers.target.wants").exists())
                if shutil.which("desktop-file-validate"):
                    subprocess.run(["desktop-file-validate", str(prefix / "share/applications/drive-sync.desktop")], check=True)
                if shutil.which("systemd-analyze"):
                    subprocess.run(["systemd-analyze", "--user", "verify",
                                    str(prefix / "config/systemd/user/drive-sync.service"),
                                    str(prefix / "config/systemd/user/drive-sync.timer")], check=True)
                (prefix / "share/drive-sync/user-file.txt").write_text("preserve")
        self.assertFalse((prefix / "bin/drive-sync").exists())
        self.assertTrue((prefix / "share/drive-sync/user-file.txt").exists())
        self.assertTrue(self.path.exists())
        self.assertTrue(self.local.exists())


if __name__ == "__main__":
    unittest.main()
