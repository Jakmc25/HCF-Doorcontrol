"""Small iCalendar reader: enough for Google Calendar's secret iCal feed.

Supports timed and all-day events, TZID/UTC times, EXDATE, modified instances
(RECURRENCE-ID) and the common RRULE forms (DAILY, WEEKLY+BYDAY, MONTHLY by
day-of-month, YEARLY, with INTERVAL / COUNT / UNTIL).
"""

import re
import threading
import time
import urllib.request
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("doorcontrol")

DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _unfold(text):
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _prop(line):
    head, _, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(s):
    return (
        s.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_dt(value, params, tz):
    """Returns (aware datetime, is_all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=tz), True
    if value.endswith("Z"):
        d = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return d.astimezone(tz), False
    d = datetime.strptime(value, "%Y%m%dT%H%M%S")
    zone = tz
    if params.get("TZID"):
        try:
            zone = ZoneInfo(params["TZID"])
        except Exception:
            zone = tz
    return d.replace(tzinfo=zone), False


def _parse_events(text, tz):
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": []}
        elif line == "END:VEVENT":
            if cur is not None and "start" in cur:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            name, params, value = _prop(line)
            try:
                if name == "DTSTART":
                    cur["start"], cur["allday"] = _parse_dt(value, params, tz)
                elif name == "DTEND":
                    cur["end"], _ = _parse_dt(value, params, tz)
                elif name == "SUMMARY":
                    cur["title"] = _unescape(value)
                elif name == "UID":
                    cur["uid"] = value
                elif name == "STATUS":
                    cur["status"] = value.upper()
                elif name == "RRULE":
                    cur["rrule"] = dict(
                        p.split("=", 1) for p in value.split(";") if "=" in p
                    )
                elif name == "EXDATE":
                    for v in value.split(","):
                        cur["exdates"].append(_parse_dt(v, params, tz)[0])
                elif name == "RECURRENCE-ID":
                    cur["recurrence_id"] = _parse_dt(value, params, tz)[0]
            except Exception as e:  # skip malformed properties, keep the event
                log.debug("ical: skipped %s: %s", name, e)
    return events


def _rrule_occurrences(start, rule):
    """Yield occurrence start datetimes in order (wall-clock times preserved across DST)."""
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", 1)))
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = None
    if "UNTIL" in rule:
        u = rule["UNTIL"]
        try:
            until, _ = _parse_dt(u, {}, start.tzinfo)
            if len(u) == 8:
                until += timedelta(days=1)
        except Exception:
            until = None
    tzinfo = start.tzinfo

    def at(d):
        return datetime(
            d.year,
            d.month,
            d.day,
            start.hour,
            start.minute,
            start.second,
            tzinfo=tzinfo,
        )

    emitted = 0

    def candidates():
        if freq == "DAILY":
            k = 0
            while True:
                yield at(start.date() + timedelta(days=k * interval))
                k += 1
        elif freq == "WEEKLY":
            byday = sorted(
                DAYS[c[-2:]] for c in rule.get("BYDAY", "").split(",") if c[-2:] in DAYS
            ) or [start.weekday()]
            week0 = start.date() - timedelta(days=start.weekday())
            w = 0
            while True:
                base = week0 + timedelta(weeks=w * interval)
                for wd in byday:
                    yield at(base + timedelta(days=wd))
                w += 1
        elif freq == "MONTHLY":
            day = (
                int(rule["BYMONTHDAY"].split(",")[0])
                if "BYMONTHDAY" in rule
                else start.day
            )
            k = 0
            while True:
                m = start.month - 1 + k * interval
                y, mo = start.year + m // 12, m % 12 + 1
                try:
                    yield at(date(y, mo, day))
                except ValueError:
                    pass
                k += 1
        elif freq == "YEARLY":
            k = 0
            while True:
                try:
                    yield at(date(start.year + k * interval, start.month, start.day))
                except ValueError:
                    pass
                k += 1
        else:
            return

    for occ in candidates():
        if occ < start:
            continue
        if until is not None and occ > until:
            return
        emitted += 1
        if count is not None and emitted > count:
            return
        yield occ


def expand(events, first, last):
    """Return [{'title','start','end'}] overlapping [first, last]."""
    overridden = {
        (e.get("uid"), e["recurrence_id"].astimezone(timezone.utc))
        for e in events
        if "recurrence_id" in e
    }
    out = []
    for e in events:
        if e.get("status") == "CANCELLED":
            continue
        start = e["start"]
        end = e.get("end") or (start + timedelta(days=1) if e.get("allday") else None)
        if end is None or end <= start:
            continue
        dur = end - start
        title = e.get("title", "")
        if "rrule" in e and "recurrence_id" not in e:
            ex = {x.astimezone(timezone.utc) for x in e["exdates"]}
            n = 0
            for occ in _rrule_occurrences(start, e["rrule"]):
                n += 1
                if occ > last or n > 20000:
                    break
                utc = occ.astimezone(timezone.utc)
                if utc in ex or (e.get("uid"), utc) in overridden:
                    continue
                if occ + dur >= first:
                    out.append({"title": title, "start": occ, "end": occ + dur})
        elif end >= first and start <= last:
            out.append({"title": title, "start": start, "end": end})
    return out


class Calendar:
    """Fetches a calendar's iCal feed and turns 'Unlock: <door>' events into windows."""

    def __init__(self, cfg, tz):
        self.url = cfg.get("ical_url", "")
        self.refresh = max(1, int(cfg.get("refresh_minutes", 10))) * 60
        self.prefix = cfg.get("title_prefix", "Unlock:").strip().casefold()
        self.before = timedelta(minutes=int(cfg.get("buffer_before_min", 0)))
        self.after = timedelta(minutes=int(cfg.get("buffer_after_min", 0)))
        self.end_action = cfg.get("end_action", "reset")
        self.tz = tz
        self.events = []
        self.last_ok = None
        self.last_error = None
        self._stop = threading.Event()

    def fetch_once(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": "door-control"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
        self.load_text(text)

    def load_text(self, text):
        self.events = _parse_events(text, self.tz)
        self.last_ok = time.time()
        self.last_error = None

    def start(self):
        if not self.url:
            return
        threading.Thread(target=self._loop, daemon=True, name="calendar").start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.fetch_once()
            except Exception as e:
                self.last_error = str(e)
                log.warning("calendar refresh failed: %s", e)
            self._stop.wait(self.refresh)

    def windows(self, first, last):
        out = []
        for occ in expand(self.events, first - self.after, last + self.before):
            title = occ["title"].strip()
            if not title.casefold().startswith(self.prefix):
                continue
            rest = title[len(self.prefix) :]
            names = [n.strip() for n in re.split(r",|&|\band\b", rest) if n.strip()]
            if not names:
                continue
            out.append(
                {
                    "title": title,
                    "start": occ["start"] - self.before,
                    "end": occ["end"] + self.after,
                    "door_queries": [
                        "*" if n.casefold() == "all" else n for n in names
                    ],
                }
            )
        return out
