#!/usr/bin/env python3
"""Install or remove the per-user package without touching sync data or config."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from drive_sync import xdg_path


def escaped_exec(path: Path, desktop: bool = False) -> str:
    text = str(path)
    # Both desktop entries and systemd interpret percent as a specifier.
    text = text.replace("%", "%%")
    if desktop:
        # Desktop Exec quoting is parsed after desktop string unescaping.
        for character in ("\\", '"', "`", "$"):
            text = text.replace(character, "\\" + character)
        text = text.replace("\\", "\\\\")
    else:
        text = text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return '"' + text + '"'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--prefix", type=Path, help="isolated package prefix (bin/, share/, config/); does not change runtime config")
    parser.add_argument("--no-systemd", action="store_true", help="do not install units or contact the user service manager")
    args = parser.parse_args()
    if os.geteuid() == 0 and not args.prefix:
        parser.error("run as your normal user, without sudo; root packaging requires --prefix")
    source = Path(__file__).resolve().parent
    if args.prefix:
        prefix = args.prefix.expanduser().resolve()
        data = prefix / "share"
        config = prefix / "config"
        binary = prefix / "bin/drive-sync"
    else:
        data = xdg_path("XDG_DATA_HOME", ".local/share")
        config = xdg_path("XDG_CONFIG_HOME", ".config")
        binary = Path.home() / ".local/bin/drive-sync"
    for path in (data, config, binary):
        if any(ord(character) < 32 for character in str(path)):
            parser.error("installation paths cannot contain control characters")
    units = config / "systemd/user"
    if args.no_systemd and not args.prefix and any((units / unit).exists() for unit in ("drive-sync.service", "drive-sync.timer")):
        parser.error("existing systemd units found; omit --no-systemd so they can be stopped safely")
    assets = {
        binary: (source / "drive_sync.py", 0o755),
        data / "icons/hicolor/scalable/apps/drive-sync.svg": (source / "icons/icon-gdrive-sync.svg", 0o644),
        data / "drive-sync/examples/config.json": (source / "examples/config.json", 0o644),
        data / "drive-sync/examples/filters.txt": (source / "examples/filters.txt", 0o644),
    }
    rendered = {
        data / "applications/drive-sync.desktop": (source / "drive-sync.desktop").read_text().replace(
            "@EXECUTABLE@", escaped_exec(binary, desktop=True)),
    }
    if not args.no_systemd:
        rendered[units / "drive-sync.service"] = (source / "drive-sync.service").read_text().replace(
            "@EXECUTABLE@", escaped_exec(binary))
        rendered[units / "drive-sync.timer"] = (source / "drive-sync.timer").read_text()
    # Custom prefixes are also useful for packaging/tests: never contact the live manager.
    manage_services = not args.no_systemd and not args.prefix
    if manage_services:
        if not shutil.which("systemctl"):
            parser.error("systemctl is missing; use --no-systemd for a manual installation")
        if subprocess.run(["systemctl", "--user", "show-environment"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=False).returncode:
            parser.error("user service manager is unavailable; use --no-systemd")
        # Replacing an installed timer must not restart synchronization before review.
        for unit in ("drive-sync.timer", "drive-sync.service"):
            if (units / unit).exists():
                subprocess.run(["systemctl", "--user", "stop", unit], check=True)
        if (units / "drive-sync.timer").exists():
            subprocess.run(["systemctl", "--user", "disable", "drive-sync.timer"], check=True)
    if args.action == "install":
        for destination, (origin, mode) in assets.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, destination)
            destination.chmod(mode)
        for destination, content in rendered.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            destination.chmod(0o644)
        print(f"Installed {binary}\nNo configuration was overwritten. No synchronization was started.")
        print("Next: drive-sync --init, edit your configuration, then follow the README for a preview.")
    else:
        for destination in [*assets, *rendered]:
            destination.unlink(missing_ok=True)
        print("Package removed. User configuration, sync data, logs and baselines were preserved.")
    if manage_services:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Package operation failed: {exc}", file=sys.stderr)
        sys.exit(1)
