"""Daily backups of everything Door Control needs to come back: the database (SQLite
backup copy, safe while the app runs), config.json (UniFi and Telegram tokens, calendar
address), accounts.json (break-glass account) and session-secret.

Written to DATA_DIR/backups/door-control-YYYYmmdd-HHMMSS.tar.gz (file 600, folder 700, the
14 newest kept). `manage.py backup` makes one on demand. Never imports web, auth or
google_signin. A failed backup never affects door control: it is logged and alerted.
"""

import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("doorcontrol")

KEEP = 14
PREFIX = "door-control-"
EXTRA_FILES = ("accounts.json", "session-secret")


def backup_dir(data_dir):
    return Path(data_dir) / "backups"


def _claim(folder, now):
    """Create a new, empty, mode-600 file with a unique name; returns (path, fd)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    n = 1
    while True:
        name = "%s%s%s.tar.gz" % (PREFIX, stamp, "" if n == 1 else "-%d" % n)
        path = folder / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            return path, fd
        except FileExistsError:
            n += 1


NAME = re.compile(r"^door-control-(\d{8}-\d{6})(?:-(\d+))?\.tar\.gz$")


def backups(data_dir):
    """Backup files, newest first (by the time in the name, then -2, -3 ...)."""
    folder = backup_dir(data_dir)
    if not folder.is_dir():
        return []
    found = []
    for p in folder.iterdir():
        m = NAME.match(p.name)
        if m and p.is_file():
            found.append(((m.group(1), int(m.group(2) or 1)), p))
    return [p for _, p in sorted(found, reverse=True)]


def run(data_dir, config_path=None, keep=KEEP, now=None):
    """Write one backup and prune old ones. Returns its path. Raises on failure."""
    data_dir = Path(data_dir)
    db_path = data_dir / "doorcontrol.db"
    if not db_path.is_file():
        raise RuntimeError("there is no database to back up yet")
    folder = backup_dir(data_dir)
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    path, fd = _claim(folder, time.time() if now is None else now)
    tmp = tempfile.mkdtemp(dir=folder)
    try:
        copy = os.path.join(tmp, "doorcontrol.db")
        src = sqlite3.connect(db_path, timeout=10)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tar:
            tar.add(copy, "doorcontrol.db")
            cfg = Path(config_path) if config_path else data_dir / "config.json"
            if cfg.is_file():
                tar.add(cfg, "config.json")
            for name in EXTRA_FILES:
                if (data_dir / name).is_file():
                    tar.add(data_dir / name, name)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for old in backups(data_dir)[keep:]:
        old.unlink()
    return path


def newest_age(data_dir, now=None):
    files = backups(data_dir)
    if not files:
        return None
    return (time.time() if now is None else now) - files[0].stat().st_mtime


def start(data_dir, config_path, notifier, every=3600, max_age=86400):
    """At start if the newest backup is older than a day, then checked hourly."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                age = newest_age(data_dir)
                if age is None or age > max_age:
                    path = run(data_dir, config_path)
                    log.info("backup written: %s", path)
                notifier.recovered("backup", "Backups work again.")
            except Exception as e:
                log.warning("backup failed: %s", e)
                notifier.outage(
                    "backup",
                    "backup",
                    "Backup problem: the daily backup failed (%s). Door control is not "
                    "affected; it tries again in an hour." % type(e).__name__,
                    key=("backup", "failed"),
                )
            stop.wait(every)

    threading.Thread(target=loop, daemon=True, name="backup").start()
    return stop
