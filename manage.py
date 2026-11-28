"""Break-glass admin accounts for the dashboard (Google sign-in is the normal way in).

Run inside the container as its normal user, never as root:
    docker compose exec app python manage.py set-password admin
    docker compose exec app python manage.py users
    docker compose exec app python manage.py remove-user admin
    docker compose exec app python manage.py backup
    docker compose exec app python manage.py sessions
    docker compose exec app python manage.py revoke jake@hardincountyfair.org
    docker compose exec app python manage.py revoke admin:admin
    docker compose exec app python manage.py revoke --all

Passwords are stored only as scrypt hashes in DATA_DIR/accounts.json (mode 600).
There is no way to create or change an account from the web.
`backup` writes the same backup as the daily one (DATA_DIR/backups, 14 kept) and prints its path.
`sessions` lists who is signed in (no session ids: only their hashes are stored); `revoke`
ends sessions at once (one email, one admin:NAME, or --all). set-password and remove-user
also end that account's sessions.
"""

import getpass
import os
import sys
import time

from doorcontrol import auth, backup
from doorcontrol.db import DB

MIN_PASSWORD = 12
USAGE = (
    "Usage: python manage.py set-password NAME | users | remove-user NAME | backup"
    " | sessions | revoke EMAIL|admin:NAME|--all"
)


def database(data_dir):
    path = os.path.join(data_dir, "doorcontrol.db")
    if not os.path.exists(path):
        raise SystemExit("No database at %s (has Door Control started yet?)." % path)
    return DB(path)


def revoke_account(data_dir, name):
    """Sessions of admin:NAME end (the app also refuses them because the gen changed)."""
    path = os.path.join(data_dir, "doorcontrol.db")
    if os.path.exists(path):
        DB(path).sessions_revoke("admin:" + name)


def accounts(data_dir):
    if os.geteuid() == 0 and os.path.isdir(data_dir):
        if os.stat(data_dir).st_uid != 0:
            # A root-owned accounts.json would be unreadable by the app user.
            raise SystemExit(
                "Run this as the app user, not root: "
                "docker compose exec app python manage.py ..."
            )
    return auth.Auth(data_dir, b"", None)


def main(argv):
    data_dir = os.environ.get("DATA_DIR", "data")
    cmd = argv[:1]
    if cmd == ["set-password"] and len(argv) == 2:
        a = accounts(data_dir)
        # From stdin when piped, so the password never shows in the process list or history.
        pw = (
            getpass.getpass("New password (12+ characters, long and random): ")
            if sys.stdin.isatty()
            else sys.stdin.readline().rstrip("\n")
        )
        if len(pw) < MIN_PASSWORD:
            raise SystemExit(
                "Use a password of at least %d characters (a long random one is best)."
                % MIN_PASSWORD
            )
        os.makedirs(data_dir, exist_ok=True)
        try:
            name = a.set_password(argv[1], pw)
        except ValueError as e:
            raise SystemExit(str(e))
        revoke_account(data_dir, name)
        print("Saved admin sign-in for", name)
    elif cmd == ["users"] and len(argv) == 1:
        names = sorted(accounts(data_dir).load())
        print("\n".join(names) or "No admin accounts yet (Google sign-in only).")
    elif cmd == ["remove-user"] and len(argv) == 2:
        try:
            a = accounts(data_dir)
            name = a.name(argv[1])
            a.remove(name)
        except ValueError as e:
            raise SystemExit(str(e))
        revoke_account(data_dir, name)
        print("Removed", argv[1])
    elif cmd == ["backup"] and len(argv) == 1:
        config = os.environ.get("DOORCONTROL_CONFIG") or os.path.join(
            data_dir, "config.json"
        )
        try:
            print(backup.run(data_dir, config))
        except Exception as e:
            raise SystemExit("Backup failed: %s" % e)
    elif cmd == ["sessions"] and len(argv) == 1:
        rows = database(data_dir).sessions_list()
        now = time.time()
        for r in rows:
            state = "expired" if r["expires"] <= now else "until"
            print(
                "%-40s %-6s %s %s"
                % (
                    r["user"],
                    r["kind"],
                    state,
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(r["expires"])),
                )
            )
        if not rows:
            print("Nobody is signed in.")
    elif cmd == ["revoke"] and len(argv) == 2:
        who = argv[1].strip()
        if not who:
            raise SystemExit(USAGE)
        n = database(data_dir).sessions_revoke(None if who == "--all" else who)
        print("Ended %d session(s)." % n)
    else:
        raise SystemExit(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
