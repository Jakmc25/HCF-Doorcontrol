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
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

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

    def set_state(self, door_id, applied, end_action=None, reason=None):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO door_state(door_id,applied,end_action,since,reason) VALUES(?,?,?,?,?)",
                (door_id, applied, end_action, time.time(), reason),
            )
            self.conn.commit()

    # ---- activity log ----
    def log(self, door, action, source, ok, detail=""):
        with self.lock:
            self.conn.execute(
                "INSERT INTO log(ts,door,action,source,ok,detail) VALUES(?,?,?,?,?,?)",
                (time.time(), door, action, source, 1 if ok else 0, detail),
            )
            self.conn.execute(
                "DELETE FROM log WHERE id <= (SELECT MAX(id) FROM log) - 1000"
            )
            self.conn.commit()

    def recent_log(self, n=40):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM log ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]
