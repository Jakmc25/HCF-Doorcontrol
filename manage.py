"""Break-glass admin accounts for the dashboard (Google sign-in is the normal way in).

Run inside the container as its normal user, never as root:
    docker compose exec app python manage.py set-password admin
    docker compose exec app python manage.py users
    docker compose exec app python manage.py remove-user admin

Passwords are stored only as scrypt hashes in DATA_DIR/accounts.json (mode 600).
There is no way to create or change an account from the web.
"""

import getpass
import os
import sys

from doorcontrol import auth

MIN_PASSWORD = 12
USAGE = "Usage: python manage.py set-password NAME | users | remove-user NAME"


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
        print("Saved admin sign-in for", name)
    elif cmd == ["users"] and len(argv) == 1:
        names = sorted(accounts(data_dir).load())
        print("\n".join(names) or "No admin accounts yet (Google sign-in only).")
    elif cmd == ["remove-user"] and len(argv) == 2:
        try:
            accounts(data_dir).remove(argv[1])
        except ValueError as e:
            raise SystemExit(str(e))
        print("Removed", argv[1])
    else:
        raise SystemExit(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
