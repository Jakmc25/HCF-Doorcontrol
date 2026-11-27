"""Scheduling engine: decides which doors should be unlocked now and applies it via UniFi lock rules.

Model: while a schedule/calendar window is active, the door gets the UniFi temporary rule
`keep_unlock`. When the window ends the door gets `reset` (back to its normal UniFi
schedule) or `keep_lock`. State is stored so a restart catches up on anything missed.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .unifi import UnifiError

log = logging.getLogger("doorcontrol")

END_ACTIONS = ("reset", "keep_lock")
ACTIONS = ("unlock_for", "keep_unlock", "lock", "keep_lock", "reset", "pulse")


class DoorError(Exception):
    pass


def parse_hm(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("Times must look like 08:30")
    return int(m.group(1)), int(m.group(2))


def validate_schedule(d):
    name = str(d.get("name", "")).strip()
    if not name:
        raise ValueError("Give the schedule a name")
    doors = d.get("doors")
    if (
        not isinstance(doors, list)
        or not doors
        or not all(isinstance(x, str) for x in doors)
    ):
        raise ValueError("Pick at least one door")
    days = d.get("days") or None
    date_s = d.get("date") or None
    if bool(days) == bool(date_s):
        raise ValueError("Choose either weekdays or one specific date")
    if days:
        if not all(isinstance(x, int) and 0 <= x <= 6 for x in days):
            raise ValueError("Invalid weekdays")
        days = sorted(set(days))
    if date_s:
        try:
            datetime.strptime(date_s, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Date must look like 2027-09-10")
    parse_hm(d.get("start"))
    parse_hm(d.get("end"))
    if parse_hm(d["start"]) == parse_hm(d["end"]):
        raise ValueError("Start and end times must differ")
    end_action = d.get("end_action", "reset")
    if end_action not in END_ACTIONS:
        raise ValueError("end_action must be reset or keep_lock")
    return {
        "name": name,
        "doors": doors,
        "days": days,
        "date": date_s,
        "start": "%02d:%02d" % parse_hm(d["start"]),
        "end": "%02d:%02d" % parse_hm(d["end"]),
        "end_action": end_action,
        "enabled": bool(d.get("enabled", True)),
    }


def schedule_occurrences(s, first_day, last_day, tz):
    """All (start, end) windows of schedule `s` that START between first_day and last_day (dates)."""
    sh, sm = parse_hm(s["start"])
    eh, em = parse_hm(s["end"])
    out = []
    d = first_day
    while d <= last_day:
        hit = (
            (s["date"] == d.isoformat())
            if s.get("date")
            else (d.weekday() in (s.get("days") or []))
        )
        if hit:
            st = datetime(d.year, d.month, d.day, sh, sm, tzinfo=tz)
            ed = (
                d if (eh, em) > (sh, sm) else d + timedelta(days=1)
            )  # overnight / "until midnight"
            en = datetime(ed.year, ed.month, ed.day, eh, em, tzinfo=tz)
            out.append((st, en))
        d += timedelta(days=1)
    return out


def _door_label(d):
    return d.get("full_name") or d.get("name") or d["id"]


def _query_matches(q, door):
    q = q.casefold()
    if q == "*":
        return True
    return (
        q in (door.get("name") or "").casefold()
        or q in (door.get("full_name") or "").casefold()
    )


class Engine:
    def __init__(self, cfg, db, client, calendar=None):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
        self.db, self.client, self.calendar = db, client, calendar
        self.default_unlock_minutes = int(cfg.get("default_unlock_minutes", 60))
        self._cache = (0.0, [])
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.last_tick = None
        self.last_error = None

    def now(self):
        return datetime.now(self.tz)

    # ---- doors ----
    def doors(self, max_age=20):
        with self._lock:
            ts, doors = self._cache
            if not doors or time.time() - ts > max_age:
                doors = self.client.doors()
                self._cache = (time.time(), doors)
            return doors

    def find_door(self, query):
        q = (query or "").strip().casefold()
        doors = self.doors()
        for d in doors:
            if (
                d["id"] == query
                or (d.get("name") or "").casefold() == q
                or (d.get("full_name") or "").casefold() == q
            ):
                return d
        matches = [d for d in doors if q and _query_matches(q, d)]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(d.get("name", d["id"]) for d in doors)
        if not matches:
            raise DoorError("No door matches '%s'. Doors: %s" % (query, names))
        raise DoorError(
            "'%s' matches several doors (%s). Be more specific."
            % (query, ", ".join(d["name"] for d in matches))
        )

    def status(self):
        out = []
        for d in self.doors(max_age=0):
            rule = {}
            try:
                rule = self.client.lock_rule(d["id"]) or {}
            except UnifiError:
                pass
            out.append(
                {
                    "id": d["id"],
                    "name": d.get("name"),
                    "full_name": d.get("full_name"),
                    "relay": d.get("door_lock_relay_status"),
                    "position": d.get("door_position_status"),
                    "rule": rule.get("type"),
                    "rule_ends": rule.get("ended_time"),
                }
            )
        return out

    # ---- actions ----
    def door_action(self, door, action, minutes=None, source="manual", user=""):
        """Apply one action to one door. Raises UnifiError on failure. Returns a short description."""
        if action not in ACTIONS:
            raise ValueError("Unknown action '%s'" % action)
        label = _door_label(door)
        try:
            if action == "unlock_for":
                minutes = max(1, min(480, int(minutes or self.default_unlock_minutes)))
                self.client.set_lock_rule(door["id"], "custom", minutes)
                desc = "unlocked for %d min" % minutes
            elif action == "keep_unlock":
                self.client.set_lock_rule(door["id"], "keep_unlock")
                desc = "kept unlocked"
            elif action == "lock":
                self.client.set_lock_rule(door["id"], "lock_now")
                desc = "locked now"
            elif action == "keep_lock":
                self.client.set_lock_rule(door["id"], "keep_lock")
                desc = "kept locked"
            elif action == "reset":
                self.client.set_lock_rule(door["id"], "reset")
                desc = "back to its normal UniFi schedule"
            else:
                self.client.unlock(door["id"])
                desc = "opened (momentary unlock)"
        except UnifiError as e:
            self.db.log(label, action, source, False, str(e), user=user)
            raise
        self.db.log(label, action, source, True, desc, user=user)
        return desc

    # ---- windows ----
    def _windows(self, doors, first_day, last_day, now):
        out = []
        for s in self.db.schedules():
            if not s["enabled"]:
                continue
            targets = {
                d["id"] for d in doors if "*" in s["doors"] or d["id"] in s["doors"]
            }
            if not targets:
                continue
            for st, en in schedule_occurrences(s, first_day, last_day, self.tz):
                out.append(
                    {
                        "start": st,
                        "end": en,
                        "source": "schedule",
                        "title": s["name"],
                        "end_action": s["end_action"],
                        "door_ids": targets,
                    }
                )
        if self.calendar:
            first = datetime(
                first_day.year, first_day.month, first_day.day, tzinfo=self.tz
            )
            last = datetime(
                last_day.year, last_day.month, last_day.day, tzinfo=self.tz
            ) + timedelta(days=1)
            for w in self.calendar.windows(first, last):
                targets = {
                    d["id"]
                    for d in doors
                    if any(_query_matches(q, d) for q in w["door_queries"])
                }
                if targets:
                    out.append(
                        {
                            "start": w["start"],
                            "end": w["end"],
                            "source": "calendar",
                            "title": w["title"],
                            "end_action": self.calendar.end_action,
                            "door_ids": targets,
                        }
                    )
        return out

    def upcoming(self, days=7):
        now = self.now()
        doors = self.doors()
        names = {d["id"]: d.get("name", d["id"]) for d in doors}
        wins = self._windows(
            doors,
            now.date() - timedelta(days=1),
            now.date() + timedelta(days=days),
            now,
        )
        wins = [
            w
            for w in wins
            if w["end"] > now and w["start"] < now + timedelta(days=days)
        ]
        wins.sort(key=lambda w: w["start"])
        return [
            {
                "start": w["start"].isoformat(),
                "end": w["end"].isoformat(),
                "source": w["source"],
                "title": w["title"],
                "active": w["start"] <= now < w["end"],
                "doors": sorted(names[i] for i in w["door_ids"]),
            }
            for w in wins
        ]

    # ---- reconcile loop ----
    def tick(self):
        now = self.now()
        doors = self.doors()
        wins = self._windows(doors, now.date() - timedelta(days=1), now.date(), now)
        for d in doors:
            label = _door_label(d)
            active = [
                w
                for w in wins
                if d["id"] in w["door_ids"] and w["start"] <= now < w["end"]
            ]
            st = self.db.get_state(d["id"])
            applied = st["applied"] if st else "free"
            try:
                if active and applied != "unlock":
                    end_action = (
                        "keep_lock"
                        if any(w["end_action"] == "keep_lock" for w in active)
                        else "reset"
                    )
                    title = active[0]["title"]
                    self.door_action(d, "keep_unlock", source="scheduler: " + title)
                    self.db.set_state(d["id"], "unlock", end_action, title)
                elif not active and applied == "unlock":
                    # Only touch the door if it is still in the state we put it in; if someone
                    # changed it by hand since, leave their change alone.
                    rule = self.client.lock_rule(d["id"]) or {}
                    if rule.get("type") == "keep_unlock":
                        self.door_action(
                            d,
                            st.get("end_action") or "reset",
                            source="scheduler: window ended",
                        )
                    else:
                        self.db.log(
                            label,
                            "window ended",
                            "scheduler",
                            True,
                            "door was changed by hand (%s); left as is"
                            % rule.get("type"),
                        )
                    self.db.set_state(d["id"], "free")
            except UnifiError as e:
                log.warning("tick: %s: %s (will retry)", label, e)
        self.last_tick = time.time()

    def run_forever(self, interval=30):
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as e:  # keep the loop alive no matter what
                self.last_error = str(e)
                log.warning("scheduler tick failed: %s", e)
            self._stop.wait(interval)

    def start(self, interval=30):
        threading.Thread(
            target=self.run_forever, args=(interval,), daemon=True, name="scheduler"
        ).start()

    def stop(self):
        self._stop.set()
