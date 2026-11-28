"""Scheduling engine: decides which doors should be unlocked now and applies it via UniFi lock rules.

Model: while a schedule/calendar window is active, the door gets the UniFi temporary rule
`keep_unlock`. When the window ends the door gets `reset` (back to its normal UniFi
schedule) or `keep_lock`, chosen at that moment from the windows active just before it
ended. State (applied rule, window source, end time) is stored so a restart catches up.

Fail-safe rules: never guess; on doubt keep the current state, log and alert; a door changed
by hand is never touched again for that window. All window times are compared in UTC.
Only one tick runs at a time. This module never imports web, auth or google_signin.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .ical import gap_end, wall_to_utc
from .notify import Notifier
from .unifi import UnifiError, UnifiUnreachable

log = logging.getLogger("doorcontrol")

UTC = timezone.utc
END_ACTIONS = ("reset", "keep_lock")
ACTIONS = ("unlock_for", "keep_unlock", "lock", "keep_lock", "reset", "pulse")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ACTION_WORDS = {
    "unlock_for": "unlock",
    "keep_unlock": "keep unlocked",
    "lock": "lock",
    "keep_lock": "keep locked",
    "reset": "put back to its normal schedule",
    "pulse": "open",
}


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
    """All (start, end) local wall-clock windows of schedule `s` that START between
    first_day and last_day (dates). fold=0: an ambiguous time is its first occurrence.
    """
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


_SPLIT = re.compile(r",|&|\band\b", re.I)


def match_doors(text, doors):
    """Calendar door text -> (set of door ids, list of problems).

    1. The whole text equals a door name or full name (any case): that door, not split.
    2. Else split on commas, "&" and "and"; each part: an exact name, else a partial match
       that hits exactly one door. A part matching none or several doors opens nothing
       (reported); "ALL" and "*" never open anything. Other parts still apply.
    """

    def exact(q):
        q = q.casefold()
        return [
            d
            for d in doors
            if q
            in ((d.get("name") or "").casefold(), (d.get("full_name") or "").casefold())
        ]

    t = (text or "").strip()
    whole = exact(t) if t else []
    if len(whole) == 1:
        return {whole[0]["id"]}, []
    parts = [p.strip() for p in _SPLIT.split(t) if p.strip()]
    if not parts:
        return set(), ["names no door"]
    ids, problems = set(), []
    for p in parts:
        if p.casefold() in ("all", "*"):
            problems.append(
                "'%s' would open every door; the calendar never does that" % p
            )
            continue
        m = exact(p)
        if not m:
            m = [d for d in doors if _query_matches(p, d)]
        if len(m) == 1:
            ids.add(m[0]["id"])
        elif not m:
            problems.append("'%s' matches no door" % p)
        else:
            problems.append(
                "'%s' matches several doors (%s)"
                % (p, ", ".join(d.get("name") or d["id"] for d in m))
            )
    return ids, problems


class Engine:
    def __init__(self, cfg, db, client, calendar=None, notifier=None):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
        self.db, self.client, self.calendar = db, client, calendar
        self.notifier = notifier or Notifier()
        self.default_unlock_minutes = int(cfg.get("default_unlock_minutes", 60))
        self.tick_seconds = int(cfg.get("tick_seconds", 30))
        self.hand_check_seconds = max(1, int(cfg.get("hand_check_minutes", 5))) * 60
        self.failures_before_alert = max(
            1, int((cfg.get("alerts") or {}).get("failures_before_alert", 2))
        )
        self.log_days = max(1, int(cfg.get("log_days", 90)))
        self.clock = time.time  # real time: tick bookkeeping, watchdog
        self._cache = (0.0, [])
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._rerun = threading.Event()
        self._stop = threading.Event()
        self._failing = {}  # (door id, action) -> {"count", "alerted"}
        self._hand_checked = {}  # door id -> UTC epoch of the last check
        self._reported = set()
        self._report_lock = threading.Lock()
        self._unifi_failures = 0
        self._last_prune = 0.0
        self._thread = None
        self._started = None
        self._stalled = False
        self.last_tick = None
        self.last_error = None

    def now(self):
        return datetime.now(self.tz)

    @property
    def lookback_days(self):
        return max(1, self.calendar.lookback_days if self.calendar else 1)

    def _fmt(self, dt):
        return dt.astimezone(self.tz).strftime("%a %I:%M %p").replace(" 0", " ")

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
    def door_action(
        self,
        door,
        action,
        minutes=None,
        source="manual",
        user="",
        quiet_repeat=False,
        until=None,
        note="",
    ):
        """Apply one action to one door. Raises UnifiError on failure. Returns a short
        description. Every door action of every source goes through here: activity log
        and Telegram alert. `quiet_repeat` (scheduler only): one log row per door and
        action per outage, alert after `failures_before_alert` tries, one recovery row.
        """
        if action not in ACTIONS:
            raise ValueError("Unknown action '%s'" % action)
        label = _door_label(door)
        who = user or source
        key = (door["id"], action)
        try:
            if action == "unlock_for":
                minutes = max(1, min(480, int(minutes or self.default_unlock_minutes)))
                self.client.set_lock_rule(door["id"], "custom", minutes)
                desc = "unlocked for %d min" % minutes
                until = minutes
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
            self._action_failed(door, label, action, source, user, who, e, quiet_repeat)
            raise
        logged = desc + note
        if quiet_repeat and key in self._failing:
            f = self._failing.pop(key)
            logged += " (worked again after %d failed tries)" % f["count"]
            if f["alerted"]:
                self.notifier.alert(
                    "recovered", "%s: %s again (%s)." % (label, desc, who)
                )
        self.db.log(label, action, source, True, logged, user=user)
        self._status_alert(label, desc, until, who)
        return desc

    def _status_alert(self, label, desc, until, who):
        """AC31. An alert problem never affects the door action that already happened."""
        try:
            if isinstance(until, int):
                until = self.now() + timedelta(minutes=until)
            end = " until %s" % self._fmt(until) if until else ""
        except Exception:
            end = ""
        self.notifier.alert("status", "%s: %s%s. By %s." % (label, desc, end, who))

    def _action_failed(self, door, label, action, source, user, who, e, quiet):
        key = (door["id"], action)
        text = "Door problem: %s could not be told to %s (%s)." % (
            label,
            ACTION_WORDS.get(action, action),
            e,
        )
        if not quiet:
            self.db.log(label, action, source, False, str(e), user=user)
            self.notifier.alert(
                "door_error",
                text + " Asked by %s. Nothing was changed; try again or use the UniFi "
                "Access app." % who,
                key=("door", door["id"], action),
            )
            return
        f = self._failing.get(key)
        if f is None:
            f = self._failing[key] = {"count": 0, "alerted": False}
            self.db.log(label, action, source, False, str(e), user=user)
        f["count"] += 1
        if f["count"] >= self.failures_before_alert and not f["alerted"]:
            f["alerted"] = True
            self.notifier.alert(
                "door_error",
                text
                + " For %s. The scheduler keeps the door as it is and retries every "
                "%d s." % (who, self.tick_seconds),
                key=("door", door["id"], action),
            )

    # ---- schedules (every change is logged and alerted with who did it) ----
    def _describe(self, s):
        names = {d["id"]: d.get("name") or d["id"] for d in self._cache[1]}
        doors = (
            "all doors"
            if "*" in s["doors"]
            else ", ".join(names.get(i, i) for i in s["doors"])
        )
        when = s["date"] or ", ".join(DAY_NAMES[d] for d in (s["days"] or []))
        return "%s %s-%s, %s" % (when, s["start"], s["end"], doors)

    def _schedule_note(self, action, detail, user, source, text):
        self.db.log("", action, source, True, detail, user=user, kind="schedule")
        self.notifier.alert("schedule_change", text)

    def add_schedule(self, s, user="", source="web"):
        sid = self.db.add_schedule(s)
        self._schedule_note(
            "schedule add",
            'added schedule "%s"' % s["name"],
            user,
            source,
            'Schedule "%s" added by %s: %s.'
            % (s["name"], user or source, self._describe(s)),
        )
        return sid

    def update_schedule(self, sid, new, user="", source="web"):
        old = self.db.get_schedule(sid)
        if not old or not self.db.update_schedule(sid, new):
            raise LookupError("No such schedule")
        who = user or source
        same = all(old.get(k) == v for k, v in new.items() if k != "enabled")
        if same and old["enabled"] != new["enabled"]:
            verb = "resume" if new["enabled"] else "pause"
            self._schedule_note(
                "schedule " + verb,
                '%sd schedule "%s"' % (verb, new["name"]),
                user,
                source,
                'Schedule "%s" %sd by %s.' % (new["name"], verb, who),
            )
        else:
            if old["name"] != new["name"]:
                detail = 'changed schedule "%s" (now "%s")' % (old["name"], new["name"])
            else:
                detail = 'changed schedule "%s"' % new["name"]
            self._schedule_note(
                "schedule edit",
                detail,
                user,
                source,
                'Schedule "%s" changed by %s. Now: "%s" %s%s.'
                % (
                    old["name"],
                    who,
                    new["name"],
                    self._describe(new),
                    "" if new["enabled"] else " (paused)",
                ),
            )
        return True

    def delete_schedule(self, sid, user="", source="web"):
        old = self.db.get_schedule(sid)
        if not old or not self.db.delete_schedule(sid):
            raise LookupError("No such schedule")
        self._schedule_note(
            "schedule delete",
            'deleted schedule "%s"' % old["name"],
            user,
            source,
            'Schedule "%s" deleted by %s.' % (old["name"], user or source),
        )
        return True

    # ---- windows ----
    def _report(self, key, kind, text):
        """Log and alert a skipped window once per process (the notifier also dedupes)."""
        with self._report_lock:
            if key in self._reported:
                return
            if len(self._reported) > 10000:
                self._reported.clear()
            self._reported.add(key)
        log.warning(text)
        self.notifier.alert(kind, text, key=key)

    def _windows(self, doors, first_day, last_day):
        """Windows with UTC start/end. Schedules are always built, whatever the calendar
        does."""
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
                su = wall_to_utc(st)
                if su is None:
                    self._report(
                        ("schedule", s["id"], st.date().isoformat()),
                        "scheduler",
                        'Schedule "%s" starts at %s on %s, a time that does not exist '
                        "when the clocks spring forward. It does not run that day."
                        % (s["name"], s["start"], st.date().isoformat()),
                    )
                    continue
                out.append(
                    {
                        "start": su,
                        "end": wall_to_utc(en) or gap_end(en),
                        "source": "schedule",
                        "title": s["name"],
                        "end_action": s["end_action"],
                        "door_ids": targets,
                        "organizer": "",
                    }
                )
        if self.calendar:
            try:
                out.extend(self._calendar_windows(doors, first_day, last_day))
            except Exception as e:
                log.exception("calendar windows failed")
                self._report(
                    ("calendar", "windows", type(e).__name__),
                    "calendar",
                    "Calendar problem: the calendar events could not be read (%s). "
                    "Schedules keep working." % type(e).__name__,
                )
        return out

    def _calendar_windows(self, doors, first_day, last_day):
        first = datetime(first_day.year, first_day.month, first_day.day, tzinfo=self.tz)
        last = datetime(
            last_day.year, last_day.month, last_day.day, tzinfo=self.tz
        ) + timedelta(days=1)
        out = []
        for w in self.calendar.windows(first, last):
            ids, problems = match_doors(w["door_text"], doors)
            for p in problems:
                self._report(
                    ("calendar", w["uid"], p),
                    "calendar",
                    'Calendar event "%s" (organizer %s): %s. That part opens no door.'
                    % (w["title"][:120], w["organizer"] or "none", p),
                )
            if ids:
                out.append(
                    {
                        "start": w["start"].astimezone(UTC),
                        "end": w["end"].astimezone(UTC),
                        "source": "calendar",
                        "title": w["title"],
                        "end_action": self.calendar.end_action,
                        "door_ids": ids,
                        "organizer": w["organizer"],
                    }
                )
        return out

    def upcoming(self, days=7):
        now_local = self.now()
        now = now_local.astimezone(UTC)
        doors = self.doors()
        names = {d["id"]: d.get("name", d["id"]) for d in doors}
        wins = self._windows(
            doors,
            now_local.date() - timedelta(days=self.lookback_days),
            now_local.date() + timedelta(days=days),
        )
        wins = [
            w
            for w in wins
            if w["end"] > now and w["start"] < now + timedelta(days=days)
        ]
        wins.sort(key=lambda w: w["start"])
        return [
            {
                "start": w["start"].astimezone(self.tz).isoformat(),
                "end": w["end"].astimezone(self.tz).isoformat(),
                "source": w["source"],
                "title": w["title"],
                "active": w["start"] <= now < w["end"],
                "doors": sorted(names[i] for i in w["door_ids"] if i in names),
            }
            for w in wins
        ]

    # ---- reconcile loop ----
    def tick(self):
        """One reconcile pass. Only one runs at a time (the loop and the web share it)."""
        with self._tick_lock:
            self._rerun.clear()
            self._tick()
            self.last_tick = self.clock()  # completed (the watchdog watches this)
        self._run_pending()

    def request_tick(self):
        """Web: run a tick soon after a change. If one is running, it runs once more
        right after, instead of a second tick at the same time."""
        self._rerun.set()
        self._run_pending()

    def _run_pending(self):
        while self._rerun.is_set():
            if not self._tick_lock.acquire(blocking=False):
                return  # the running tick sees _rerun when it finishes
            try:
                self._rerun.clear()
                self._tick()
                self.last_tick = self.clock()
            except Exception as e:
                log.warning("tick after change failed: %s", e)
            finally:
                self._tick_lock.release()

    def _tick(self):
        now_local = self.now()
        now = now_local.astimezone(UTC)
        cal_state = self.calendar.check_health() if self.calendar else "off"
        try:
            doors = self.doors()
        except UnifiError as e:
            self._unifi_down(e)
            self.last_tick = self.clock()  # a handled outage is a completed tick
            raise
        self._unifi_up()
        trusted = cal_state in ("off", "ok")
        wins = self._windows(
            doors,
            now_local.date() - timedelta(days=self.lookback_days),
            now_local.date(),
        )
        for d in doors:
            label = _door_label(d)
            try:
                self._door_tick(d, wins, now, trusted)
            except UnifiUnreachable as e:
                log.warning(
                    "tick: %s: %s (will retry; other doors next tick)", label, e
                )
                break
            except UnifiError as e:
                log.warning("tick: %s: %s (will retry)", label, e)
            except Exception as e:
                log.exception("tick: %s failed", label)
                self.notifier.alert(
                    "scheduler",
                    "Scheduler problem with %s (%s). The other doors were handled; "
                    "this door is tried again in %d s."
                    % (label, type(e).__name__, self.tick_seconds),
                    key=("door", d["id"], "error"),
                )
        self._maybe_prune()

    def _unifi_down(self, e):
        self._unifi_failures += 1
        if self._unifi_failures >= self.failures_before_alert:
            self.notifier.outage(
                "unifi",
                "door_error",
                "UniFi problem: %s Doors keep their current state; Door Control retries "
                "every %d s. Use the UniFi Access app for urgent changes."
                % (e, self.tick_seconds),
                key=("unifi", "down"),
            )

    def _unifi_up(self):
        if self._unifi_failures:
            self._unifi_failures = 0
            self.notifier.recovered(
                "unifi", "The UniFi door controller can be reached again."
            )

    def _door_tick(self, d, wins, now, trusted):
        mine = [w for w in wins if d["id"] in w["door_ids"]]
        active = [
            w
            for w in mine
            if w["start"] <= now < w["end"] and (trusted or w["source"] != "calendar")
        ]
        st = self.db.get_state(d["id"]) or {}
        if (st.get("applied") or "free") != "unlock":
            if active:
                self._start_window(d, active, now)
            return
        if active:
            ends = max(w["end"] for w in active).timestamp()
            src = (
                "schedule"
                if any(w["source"] == "schedule" for w in active)
                else "calendar"
            )
            if st.get("ends_at") != ends or st.get("source") != src:
                self.db.update_state(d["id"], ends_at=ends, source=src)
            self._hand_check(d, st, now)
            return
        if (
            st.get("source") == "calendar"
            and not trusted
            and st.get("ends_at")
            and now.timestamp() < st["ends_at"]
        ):
            # Calendar not read yet or stale: leave the door as it is until the end
            # recorded when it opened (never extended, never ended early).
            self._hand_check(d, st, now)
            return
        self._end_window(d, st, mine, now)

    def _start_window(self, d, active, now):
        sched = [w for w in active if w["source"] == "schedule"]
        w0 = (sched or active)[0]
        ends = max(w["end"] for w in active)
        if w0["source"] == "calendar":
            source, user = "calendar: " + w0["title"], "calendar:" + w0["organizer"]
            note = ' (event "%s")' % w0["title"]
        else:
            source, user, note = "scheduler: " + w0["title"], "", ""
        self.door_action(
            d,
            "keep_unlock",
            source=source,
            user=user,
            quiet_repeat=True,
            until=ends,
            note=note,
        )
        end_action = (
            "keep_lock"
            if any(w["end_action"] == "keep_lock" for w in active)
            else "reset"
        )
        self.db.set_state(
            d["id"],
            "unlock",
            end_action,
            w0["title"],
            since=now.timestamp(),
            source="schedule" if sched else "calendar",
            ends_at=ends.timestamp(),
        )
        self._hand_checked[d["id"]] = now.timestamp()

    def _end_action(self, st, mine, now):
        """Chosen when the window ends, from the windows active just before it ended."""
        since = st.get("since") or 0
        ended = [w for w in mine if since < w["end"].timestamp() <= now.timestamp()]
        if not ended:
            return st.get("end_action") or "reset"
        t = max(w["end"] for w in ended)
        before = [w for w in mine if w["start"] < t <= w["end"]]
        return (
            "keep_lock"
            if any(w["end_action"] == "keep_lock" for w in before)
            else "reset"
        )

    def _end_window(self, d, st, mine, now):
        label = _door_label(d)
        if st.get("hand_alerted"):
            self.db.log(
                label,
                "window ended",
                "scheduler",
                True,
                "door was changed by hand earlier; left as is",
            )
            self.db.set_state(d["id"], "free")
            return
        # Only touch the door if it is still in the state we put it in; if someone
        # changed it by hand since, leave their change alone.
        rule = self.client.lock_rule(d["id"]) or {}
        if rule.get("type") == "keep_unlock":
            self.door_action(
                d,
                self._end_action(st, mine, now),
                source="scheduler: window ended",
                quiet_repeat=True,
            )
        else:
            self._hand_changed(d, rule)
        self.db.set_state(d["id"], "free")
        self._hand_checked.pop(d["id"], None)

    def _hand_check(self, d, st, now):
        """While we hold a door unlocked: every hand_check_minutes, is it still ours?"""
        if st.get("hand_alerted"):
            return
        last = self._hand_checked.get(d["id"])
        if last is not None and now.timestamp() - last < self.hand_check_seconds:
            return
        self._hand_checked[d["id"]] = now.timestamp()
        rule = self.client.lock_rule(d["id"]) or {}
        if rule.get("type") != "keep_unlock":
            self._hand_changed(d, rule)
            self.db.update_state(d["id"], hand_alerted=1)

    def _hand_changed(self, d, rule):
        label = _door_label(d)
        self.db.log(
            label,
            "changed by hand",
            "scheduler",
            True,
            "door was changed by hand (%s); left as is" % rule.get("type"),
        )
        self.notifier.alert(
            "hand_change",
            "%s was changed by hand in UniFi (now: %s) while a schedule or calendar "
            "event held it unlocked. Door Control leaves it as it is."
            % (label, rule.get("type") or "unknown"),
            key=("hand", d["id"]),
        )

    def _maybe_prune(self):
        t = self.clock()
        if t - self._last_prune >= 3600:
            self._last_prune = t
            self.db.prune(self.log_days)

    # ---- loop, watchdog, health ----
    def run_forever(self, interval=30):
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except UnifiError as e:  # outage: handled, the tick counts as completed
                self.last_error = str(e)
            except Exception as e:  # keep the loop alive no matter what
                self.last_error = str(e)
                log.warning("scheduler tick failed: %s", e)
                self.notifier.alert(
                    "scheduler",
                    "Scheduler problem: a check of the doors failed (%s). Doors keep "
                    "their current state; it retries every %d s."
                    % (type(e).__name__, interval),
                    key=("scheduler", "tick"),
                )
            self._stop.wait(interval)

    def start(self, interval=30):
        self.tick_seconds = int(interval)
        self._started = self.clock()
        self._thread = threading.Thread(
            target=self.run_forever, args=(interval,), daemon=True, name="scheduler"
        )
        self._thread.start()
        threading.Thread(target=self._watch, daemon=True, name="watchdog").start()

    def _watch(self):
        while not self._stop.wait(10):
            self.check_health()

    def healthy(self, now=None):
        """False when no tick completed for 3 intervals or the scheduler thread died.
        An engine that was never started (tests, tools) counts as healthy."""
        if self._thread is None:
            return True
        now = self.clock() if now is None else now
        last = self.last_tick or self._started
        return self._thread.is_alive() and now - last <= 3 * self.tick_seconds

    def check_health(self, now=None):
        ok = self.healthy(now)
        if not ok and not self._stalled:
            self._stalled = True
            self.notifier.outage(
                "scheduler",
                "scheduler",
                "Scheduler stalled: no check of the doors finished in the last %d s. "
                "Doors keep their current state. Restart Door Control on fair-server "
                "if this does not clear." % (3 * self.tick_seconds),
                key=("scheduler", "stalled"),
            )
        elif ok and self._stalled:
            self._stalled = False
            self.notifier.recovered("scheduler", "The scheduler is running again.")
        return ok

    def stop(self):
        self._stop.set()
