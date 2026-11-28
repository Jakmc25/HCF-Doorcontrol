import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  doors TEXT NOT NULL,
  days TEXT,
  date TEXT,
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  end_action TEXT NOT NULL DEFAULT 'reset',
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS door_state(
  door_id TEXT PRIMARY KEY,
  applied TEXT NOT NULL,
  end_action TEXT,
  since REAL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, door TEXT, action TEXT, source TEXT, ok INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
  id_hash TEXT PRIMARY KEY,
  user TEXT NOT NULL,
  kind TEXT NOT NULL,
  gen TEXT,
  created REAL NOT NULL,
  expires REAL NOT NULL,
  ip TEXT
);
CREATE TABLE IF NOT EXISTS people(
  email TEXT PRIMARY KEY COLLATE NOCASE,
  added_by TEXT,
  added_at REAL
);
"""

# Log kinds (1.1.0; "people" 1.2.0). Rows are kept by age; sign-ins have their own cap so
# they never push door actions out of the log.
LOG_KINDS = ("door", "schedule", "signin", "people", "system")
SIGNIN_KEEP = 5000


class DB:
    def __init__(self, path):
        # timeout: manage.py (backup) and the app may touch the file at the same time.
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _columns(self, table):
        return {r[1] for r in self.conn.execute("PRAGMA table_info(%s)" % table)}

    def _migrate(self):
        """Additive only: never recreate a table or change existing rows."""
        cols = self._columns("log")
        if "user" not in cols:
            # Who did it (signed-in email or admin account); empty for scheduler rows.
            self.conn.execute(
                "ALTER TABLE log ADD COLUMN \"user\" TEXT NOT NULL DEFAULT ''"
            )
        if "kind" not in cols:
            # 1.1.0: door / schedule / signin / system (pruning by kind and age).
            self.conn.execute(
                "ALTER TABLE log ADD COLUMN kind TEXT NOT NULL DEFAULT 'door'"
            )
        cols = self._columns("door_state")
        # 1.1.0: which kind of window holds the door, when it ends (UTC epoch), and
        # whether a change by hand was already reported for this window.
        if "source" not in cols:
            self.conn.execute("ALTER TABLE door_state ADD COLUMN source TEXT")
        if "ends_at" not in cols:
            self.conn.execute("ALTER TABLE door_state ADD COLUMN ends_at REAL")
        if "hand_alerted" not in cols:
            self.conn.execute(
                "ALTER TABLE door_state ADD COLUMN hand_alerted INTEGER NOT NULL DEFAULT 0"
            )
        if "cal_ends_at" not in cols:
            # 1.1.0 review R1: end of the calendar window(s) holding the door (UTC epoch),
            # kept apart from ends_at so a stale or unread calendar never shortens it.
            self.conn.execute("ALTER TABLE door_state ADD COLUMN cal_ends_at REAL")

    # ---- schedules ----
    @staticmethod
    def _sched(row):
        d = dict(row)
        d["doors"] = json.loads(d["doors"])
        d["days"] = json.loads(d["days"]) if d["days"] else None
        d["enabled"] = bool(d["enabled"])
        return d

    def schedules(self):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        return [self._sched(r) for r in rows]

    def get_schedule(self, sid):
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM schedules WHERE id=?", (sid,)
            ).fetchone()
        return self._sched(r) if r else None

    def add_schedule(self, s):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO schedules(name,doors,days,date,start,end,end_action,enabled) VALUES(?,?,?,?,?,?,?,?)",
                (
                    s["name"],
                    json.dumps(s["doors"]),
                    json.dumps(s["days"]) if s["days"] else None,
                    s["date"],
                    s["start"],
                    s["end"],
                    s["end_action"],
                    1 if s["enabled"] else 0,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def update_schedule(self, sid, s):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE schedules SET name=?,doors=?,days=?,date=?,start=?,end=?,end_action=?,enabled=? WHERE id=?",
                (
                    s["name"],
                    json.dumps(s["doors"]),
                    json.dumps(s["days"]) if s["days"] else None,
                    s["date"],
                    s["start"],
                    s["end"],
                    s["end_action"],
                    1 if s["enabled"] else 0,
                    sid,
                ),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def delete_schedule(self, sid):
        with self.lock:
            cur = self.conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
            self.conn.commit()
            return cur.rowcount > 0

    # ---- per-door state the scheduler has applied ----
    def get_state(self, door_id):
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM door_state WHERE door_id=?", (door_id,)
            ).fetchone()
        return dict(r) if r else None

    def set_state(
        self,
        door_id,
        applied,
        end_action=None,
        reason=None,
        since=None,
        source=None,
        ends_at=None,
        cal_ends_at=None,
    ):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO door_state"
                "(door_id,applied,end_action,since,reason,source,ends_at,hand_alerted,"
                "cal_ends_at) VALUES(?,?,?,?,?,?,?,0,?)",
                (
                    door_id,
                    applied,
                    end_action,
                    time.time() if since is None else since,
                    reason,
                    source,
                    ends_at,
                    cal_ends_at,
                ),
            )
            self.conn.commit()

    def update_state(self, door_id, **cols):
        allowed = {"source", "ends_at", "hand_alerted", "cal_ends_at"}
        if not cols or set(cols) - allowed:
            raise ValueError("update_state: unknown column")
        names = sorted(cols)
        with self.lock:
            self.conn.execute(
                "UPDATE door_state SET %s WHERE door_id=?"
                % ",".join("%s=?" % n for n in names),
                [cols[n] for n in names] + [door_id],
            )
            self.conn.commit()

    # ---- small key/value store (Telegram update offset) ----
    def kv_get(self, key, default=None):
        with self.lock:
            r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def kv_set(self, key, value):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, str(value))
            )
            self.conn.commit()

    # ---- sign-in sessions (1.2.0): only sha256 of the cookie id is stored ----
    def session_add(self, id_hash, user, kind, gen, created, expires, ip=""):
        with self.lock:
            self.conn.execute(
                "INSERT INTO sessions(id_hash,user,kind,gen,created,expires,ip) "
                "VALUES(?,?,?,?,?,?,?)",
                (id_hash, user, kind, gen, created, expires, ip),
            )
            self.conn.commit()

    def session_get(self, id_hash):
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM sessions WHERE id_hash=?", (id_hash,)
            ).fetchone()
        return dict(r) if r else None

    def session_delete(self, id_hash):
        with self.lock:
            self.conn.execute("DELETE FROM sessions WHERE id_hash=?", (id_hash,))
            self.conn.commit()

    def sessions_list(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT user,kind,created,expires,ip FROM sessions ORDER BY created"
            ).fetchall()
        return [dict(r) for r in rows]

    def sessions_revoke(self, user=None):
        """Delete the sessions of one user (email or admin:NAME, any case), or all of
        them with user=None. Returns how many were deleted."""
        with self.lock:
            if user is None:
                cur = self.conn.execute("DELETE FROM sessions")
            else:
                cur = self.conn.execute(
                    "DELETE FROM sessions WHERE lower(user)=lower(?)", (user,)
                )
            self.conn.commit()
            return cur.rowcount

    def sessions_purge(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            cur = self.conn.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            self.conn.commit()
            return cur.rowcount

    # ---- in-app people list (1.2.0) ----
    def people(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT email,added_by,added_at FROM people ORDER BY email"
            ).fetchall()
        return [dict(r) for r in rows]

    def people_emails(self):
        with self.lock:
            rows = self.conn.execute("SELECT email FROM people").fetchall()
        return {r[0].lower() for r in rows}

    def person_add(self, email, added_by):
        """False if the email is already on the list (any case)."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO people(email,added_by,added_at) VALUES(?,?,?)",
                (email, added_by, time.time()),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def person_remove(self, email):
        """Remove a person and end their Google sessions in the same transaction.
        False if the email was not on the list."""
        with self.lock:
            try:
                cur = self.conn.execute(
                    "DELETE FROM people WHERE email=? COLLATE NOCASE", (email,)
                )
                if cur.rowcount == 0:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM sessions WHERE kind='google' AND lower(user)=lower(?)",
                    (email,),
                )
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    # ---- activity log ----
    def log(self, door, action, source, ok, detail="", user="", kind="door"):
        with self.lock:
            self.conn.execute(
                'INSERT INTO log(ts,door,action,source,ok,detail,"user",kind) '
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    door,
                    action,
                    source,
                    1 if ok else 0,
                    detail,
                    user or "",
                    kind if kind in LOG_KINDS else "door",
                ),
            )
            self.conn.commit()

    def prune(self, days=90, signin_keep=SIGNIN_KEEP, now=None):
        """Rows older than `days` go; sign-in rows are also capped at the newest
        `signin_keep`. Door actions are never pushed out by sign-ins."""
        now = time.time() if now is None else now
        with self.lock:
            self.conn.execute("DELETE FROM log WHERE ts < ?", (now - days * 86400,))
            self.conn.execute(
                "DELETE FROM log WHERE kind='signin' AND id NOT IN "
                "(SELECT id FROM log WHERE kind='signin' ORDER BY id DESC LIMIT ?)",
                (signin_keep,),
            )
            self.conn.commit()

    def recent_log(self, n=40):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM log ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]
