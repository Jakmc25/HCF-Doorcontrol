"""Small iCalendar reader: enough for Google Calendar's secret iCal feed.

Supports timed and all-day events, TZID/UTC times, EXDATE (timed, and VALUE=DATE which
cancels every occurrence on that local day), modified instances (RECURRENCE-ID) and these
RRULE forms: DAILY, WEEKLY (+ plain BYDAY), MONTHLY (BYMONTHDAY 1-31, or one nth/last
weekday: 2TU, 1MO, -1FR, or BYDAY + BYSETPOS), YEARLY, with INTERVAL / COUNT / UNTIL.
Anything else marks the event as a problem: it is skipped, logged and alerted, never
expanded as a different pattern. Expansion tries at most MAX_CANDIDATES dates per event.

Safety rules for 'Unlock:' events (see README "Google Calendar"): only CONFIRMED events from
an allowed organizer, never an unanswered invitation, never longer than max_window_hours.
All times are compared in UTC; the engine converts back to local time only for display.
"""

import calendar as _calendar
import logging
import math
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import people
from .notify import Notifier

log = logging.getLogger("doorcontrol")

UTC = timezone.utc
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_CANDIDATES = 5000
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
RRULE_PARTS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY"}
RRULE_PARTS |= {"BYSETPOS", "WKST"}
UNANSWERED = ("NEEDS-ACTION", "TENTATIVE")


class FeedError(ValueError):
    """The feed reply is not a complete calendar. Its text is safe to show (no URL)."""


def check_url(url):
    """Config check for calendar.ical_url: the secret feed must come over HTTPS."""
    if url and not str(url).startswith("https://"):
        raise ValueError(
            "calendar.ical_url must start with https:// "
            "(use Google Calendar's secret address in iCal format)."
        )


_URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S*")


def safe_title(text, limit=120):
    """An event title (or UID) for an alert or log line: links removed (review S15),
    control characters dropped, shortened."""
    t = _URL.sub("[link]", str(text or ""))
    t = "".join(c if c.isprintable() else " " for c in t).strip()
    return t[:limit]


# ---- time helpers (also used by the engine for schedules) ----
def wall_to_utc(dt):
    """Aware local wall time -> UTC, or None when that wall time does not exist (the
    spring-forward gap). An ambiguous time (fall-back) is its first occurrence."""
    dt = dt.replace(fold=0)
    u = dt.astimezone(UTC)
    if u.astimezone(dt.tzinfo).replace(tzinfo=None) != dt.replace(tzinfo=None):
        return None
    return u


def gap_end(dt):
    """A wall time inside the spring-forward gap -> the first wall minute after it that
    exists (02:30 -> 03:00), in UTC."""
    base = dt.replace(second=0, microsecond=0, fold=0)
    for i in range(1, 24 * 60):
        u = wall_to_utc(base + timedelta(minutes=i))
        if u is not None:
            return u
    return dt.astimezone(UTC)


# ---- parsing ----
def _unfold(text):
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def check_feed(text):
    """Raise FeedError unless the text is one complete VCALENDAR with balanced VEVENTs."""
    lines = [l.strip().upper() for l in _unfold(text or "") if l.strip()]
    bad = FeedError(
        "The calendar feed reply was not a complete calendar (empty, a web page or "
        "cut off). Door Control keeps the last calendar it read."
    )
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise bad
    inside = False
    for l in lines:
        if l == "BEGIN:VEVENT":
            if inside:
                raise bad
            inside = True
        elif l == "END:VEVENT":
            if not inside:
                raise bad
            inside = False
    if inside:
        raise bad


def _prop(line):
    """'NAME;A=1;B="x:y":value' -> ('NAME', {'A': '1', 'B': 'x:y'}, 'value')."""
    quoted = False
    cut = len(line)
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            cut = i
            break
    head, value = line[:cut], line[cut + 1 :]
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', head) or [""]
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.strip().upper()] = v.strip('"')
    return parts[0].strip().upper(), params, value


def _unescape(s):
    return (
        s.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_dt(value, params, tz):
    """Returns (aware datetime, is_all_day). UTC values stay in UTC; an unknown TZID is
    an error (never a silent fall back to local time)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(tzinfo=tz), True
    if value.endswith("Z"):
        d = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
        return d.replace(tzinfo=UTC), False
    d = datetime.strptime(value, "%Y%m%dT%H%M%S")
    zone = tz
    if params.get("TZID"):
        try:
            zone = ZoneInfo(params["TZID"])
        except Exception:
            raise ValueError("unknown time zone %r" % params["TZID"][:40])
    return d.replace(tzinfo=zone), False


def _email(value):
    v = value.strip()
    if v.lower().startswith("mailto:"):
        v = v[7:]
    return v.strip().lower()


def _new_event():
    return {
        "exdates": set(),
        "exdays": set(),
        "attendees": [],
        "problems": [],
    }


def _finish_event(cur, tz):
    """Validate the repeat rule once DTSTART is known."""
    raw = cur.pop("rrule_raw", None)
    if raw is not None and "start" in cur:
        try:
            cur["rrule"] = validate_rrule(raw, cur["start"], tz)
        except ValueError as e:
            cur["problems"].append(str(e))
    if "start" not in cur:
        cur["problems"].append("has no start time")


def _parse_events(text, tz):
    events, cur, depth = [], None, 0
    for line in _unfold(text):
        line = line.rstrip()
        up = line.upper()
        if up == "BEGIN:VEVENT":
            cur, depth = _new_event(), 0
            continue
        if cur is None:
            continue
        if up == "END:VEVENT":
            _finish_event(cur, tz)
            if "start" in cur or cur.get("title"):
                events.append(cur)
            cur = None
            continue
        # Properties of nested parts (VALARM: its ATTENDEE is an alarm address) are skipped.
        if up.startswith("BEGIN:"):
            depth += 1
            continue
        if up.startswith("END:"):
            depth = max(0, depth - 1)
            continue
        if depth or ":" not in line:
            continue
        name, params, value = _prop(line)
        try:
            if name == "DTSTART":
                cur["start"], cur["allday"] = _parse_dt(value, params, tz)
            elif name == "DTEND":
                cur["end"], _ = _parse_dt(value, params, tz)
            elif name == "SUMMARY":
                cur["title"] = _unescape(value)
            elif name == "UID":
                cur["uid"] = value.strip()
            elif name == "STATUS":
                cur["status"] = value.strip().upper()
            elif name == "ORGANIZER":
                cur["organizer"] = _email(value)
            elif name == "ATTENDEE":
                partstat = (params.get("PARTSTAT") or "NEEDS-ACTION").upper()
                cur["attendees"].append((_email(value), partstat))
            elif name == "RRULE":
                raw = {}
                for p in value.split(";"):
                    if p.strip():
                        k, sep, v = p.partition("=")
                        if not sep:
                            raise ValueError("bad repeat rule")
                        raw[k.strip().upper()] = v.strip()
                cur["rrule_raw"] = raw
            elif name == "EXDATE":
                for v in value.split(","):
                    dt, allday = _parse_dt(v, params, tz)
                    if allday:
                        cur["exdays"].add(dt.date())
                    else:
                        cur["exdates"].add(dt.replace(fold=0).astimezone(UTC))
            elif name == "RECURRENCE-ID":
                dt, _ = _parse_dt(value, params, tz)
                cur["recurrence_id"] = dt.replace(fold=0).astimezone(UTC)
        except Exception as e:
            cur["problems"].append("could not read %s (%s)" % (name, e))
    return events


# ---- repeat rules ----
def _posint(raw, key, hi):
    v = raw.get(key)
    if v is None:
        return None
    if not re.fullmatch(r"\d{1,6}", v) or not 1 <= int(v) <= hi:
        raise ValueError("repeat rule has an invalid %s (%s)" % (key, v[:20]))
    return int(v)


def validate_rrule(raw, start, tz):
    """Parsed RRULE parts -> normalized rule, or ValueError naming what is unsupported."""
    extra = set(raw) - RRULE_PARTS
    if extra:
        raise ValueError(
            "repeat rule uses %s, which Door Control does not support"
            % ", ".join(sorted(extra))
        )
    freq = raw.get("FREQ", "").upper()
    if freq not in FREQS:
        raise ValueError("repeat rule frequency %r is not supported" % freq[:20])
    rule = {
        "freq": freq,
        "interval": _posint(raw, "INTERVAL", 1000) or 1,
        "count": _posint(raw, "COUNT", 10000),
        "until": None,
        "days": None,
        "monthdays": None,
        "nth": None,
    }
    if "UNTIL" in raw:
        u = raw["UNTIL"]
        try:
            dt, allday = _parse_dt(u, {}, start.tzinfo or tz)
        except Exception:
            raise ValueError("repeat rule end date (UNTIL) could not be read")
        if allday:
            nxt = dt.date() + timedelta(days=1)
            dt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=dt.tzinfo)
            rule["until"] = (wall_to_utc(dt) or gap_end(dt)) - timedelta(seconds=1)
        else:
            rule["until"] = dt.replace(fold=0).astimezone(UTC)
    byday = raw.get("BYDAY", "").upper()
    bymd = raw.get("BYMONTHDAY", "")
    bysp = raw.get("BYSETPOS", "")
    if freq in ("DAILY", "YEARLY"):
        if byday or bymd or bysp:
            raise ValueError("repeat rule options with %s are not supported" % freq)
    elif freq == "WEEKLY":
        if bymd or bysp:
            raise ValueError("repeat rule options with WEEKLY are not supported")
        if byday:
            codes = byday.split(",")
            if not all(c in DAYS for c in codes):
                raise ValueError(
                    "weekly repeat on %s is not supported (plain weekdays only)"
                    % byday[:30]
                )
            rule["days"] = sorted({DAYS[c] for c in codes})
    else:  # MONTHLY
        if bymd and (byday or bysp):
            raise ValueError("monthly repeat mixing day numbers and weekdays")
        if bymd:
            vals = bymd.split(",")
            if not all(re.fullmatch(r"\d{1,2}", v) and 1 <= int(v) <= 31 for v in vals):
                raise ValueError(
                    "monthly repeat on day %s is not supported (1 to 31 only)"
                    % bymd[:30]
                )
            rule["monthdays"] = sorted({int(v) for v in vals})
        elif byday:
            m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", byday)
            if not m or (m.group(1) and bysp):
                raise ValueError("monthly repeat on %s is not supported" % byday[:30])
            n = m.group(1) or bysp
            if (
                not n
                or not re.fullmatch(r"[+-]?\d", n)
                or int(n) not in (1, 2, 3, 4, -1)
            ):
                raise ValueError(
                    "monthly repeat on %s is not supported (use 1st-4th or last weekday)"
                    % (byday + (";BYSETPOS=" + bysp if bysp else ""))[:40]
                )
            rule["nth"] = (int(n), DAYS[m.group(2)])
        elif bysp:
            raise ValueError("monthly repeat with BYSETPOS only is not supported")
    return rule


def _safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _nth_weekday(y, m, n, wd):
    if n > 0:
        first = date(y, m, 1)
        return first + timedelta(days=(wd - first.weekday()) % 7 + 7 * (n - 1))
    last = date(y, m, _calendar.monthrange(y, m)[1])
    return last - timedelta(days=(last.weekday() - wd) % 7)


def _candidate_days(d0, rule):
    """Candidate dates in order (None for an impossible date such as 31 April)."""
    f, iv = rule["freq"], rule["interval"]
    week0 = d0 - timedelta(days=d0.weekday())
    k = 0
    while True:
        if f == "DAILY":
            yield d0 + timedelta(days=k * iv)
        elif f == "WEEKLY":
            base = week0 + timedelta(weeks=k * iv)
            for wd in rule["days"] or [d0.weekday()]:
                yield base + timedelta(days=wd)
        elif f == "MONTHLY":
            m = d0.month - 1 + k * iv
            y, mo = d0.year + m // 12, m % 12 + 1
            if rule["nth"]:
                yield _nth_weekday(y, mo, *rule["nth"])
            else:
                for md in rule["monthdays"] or [d0.day]:
                    yield _safe_date(y, mo, md)
        else:
            yield _safe_date(d0.year + k * iv, d0.month, d0.day)
        k += 1


def rrule_occurrences(start, rule):
    """Occurrence start times (wall clock, start's time zone) in order. Raises
    RuntimeError after MAX_CANDIDATES candidate dates, so no rule can loop forever."""
    d0 = start.date()
    tried = emitted = 0
    for d in _candidate_days(d0, rule):
        tried += 1
        if tried > MAX_CANDIDATES:
            raise RuntimeError("repeat rule needs too many steps to expand")
        if d is None or d < d0:
            continue
        occ = datetime(
            d.year,
            d.month,
            d.day,
            start.hour,
            start.minute,
            start.second,
            tzinfo=start.tzinfo,
        )
        if rule["until"] is not None and occ.astimezone(UTC) > rule["until"]:
            return
        emitted += 1
        if rule["count"] is not None and emitted > rule["count"]:
            return
        yield occ


def _midnight_utc(d, tz):
    dt = datetime(d.year, d.month, d.day, tzinfo=tz)
    return wall_to_utc(dt) or gap_end(dt)


def event_span(e):
    """(all-day days, timed duration) of an event; ValueError when it makes no sense."""
    start, end = e["start"], e.get("end")
    if e.get("allday"):
        days = 1 if end is None else (end.date() - start.date()).days
        if days < 1:
            raise ValueError("ends before it starts")
        return days, None
    if end is None:
        raise ValueError("has no end time")
    dur = end.replace(fold=0).astimezone(UTC) - start.replace(fold=0).astimezone(UTC)
    if dur <= timedelta(0):
        raise ValueError("ends before it starts")
    return None, dur


def expand_event(e, first, last, overridden=frozenset()):
    """Occurrences of one event overlapping [first, last] (UTC).

    Returns ([(start_utc, end_utc)], [local dates skipped because the start time does not
    exist that day (spring forward)]).
    """
    start = e["start"]
    days, dur = event_span(e)

    def window(wall):
        if days:
            d = wall.date()
            return (
                _midnight_utc(d, wall.tzinfo),
                _midnight_utc(d + timedelta(days=days), wall.tzinfo),
            )
        s = wall_to_utc(wall)
        return None if s is None else (s, s + dur)

    out, gaps = [], []
    if "rrule" not in e or "recurrence_id" in e:
        w = window(start)
        if w is None:
            if start.astimezone(UTC) + dur > first:
                gaps.append(start.date())
        elif w[1] > first and w[0] <= last:
            out.append(w)
        return out, gaps
    uid = e.get("uid")
    for occ in rrule_occurrences(start, e["rrule"]):
        approx = occ.astimezone(UTC)
        if approx > last:
            break
        if occ.date() in e["exdays"]:
            continue
        w = window(occ)
        if w is None:
            if approx + dur > first:
                gaps.append(occ.date())
            continue
        if w[0] in e["exdates"] or (uid, w[0]) in overridden:
            continue
        if w[1] > first:
            out.append(w)
    return out, gaps


# ---- the calendar ----
class Calendar:
    """Fetches a calendar's iCal feed and turns accepted 'Unlock: <door>' events into
    windows. Never trusts an event it cannot fully understand."""

    def __init__(self, cfg, tz, notifier=None, organizers=None, clock=time.time):
        self.cfg = cfg
        self.url = cfg.get("ical_url", "")
        self.refresh = max(1, int(cfg.get("refresh_minutes", 10))) * 60
        self.prefix = cfg.get("title_prefix", "Unlock:").strip().casefold()
        self.before = timedelta(minutes=int(cfg.get("buffer_before_min", 0)))
        self.after = timedelta(minutes=int(cfg.get("buffer_after_min", 0)))
        self.end_action = cfg.get("end_action", "reset")
        self.stale_seconds = max(1.0, float(cfg.get("stale_minutes", 60))) * 60
        # Never more than 24 hours, whatever config.json says (review S11).
        self.max_window_hours = min(
            24.0, max(1.0, float(cfg.get("max_window_hours", 24)))
        )
        self.calendar_email = str(cfg.get("calendar_email") or "").strip().lower()
        self.tz = tz
        self.notifier = notifier or Notifier()
        self.clock = clock
        self._organizers = organizers
        self.events = []
        self.last_ok = None
        self.last_error = None
        self._reported = set()
        self._lock = threading.Lock()
        self._stale_alerted = False
        self._stop = threading.Event()

    @property
    def lookback_days(self):
        """Days back the engine reads windows from, so any allowed window is seen."""
        return int(math.ceil(self.max_window_hours / 24.0)) + 1

    def organizers(self):
        o = self._organizers
        if o is None:
            return people.allowed_organizers(self.cfg)
        return {str(x).lower() for x in (o() if callable(o) else o)}

    # ---- feed ----
    def fetch_once(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": "door-control"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = r.headers.get_content_type() if r.headers else ""
            raw = r.read(MAX_FEED_BYTES + 1)
        if len(raw) > MAX_FEED_BYTES:
            raise ValueError("calendar feed is larger than 5 MB")
        if ctype == "text/html":
            raise FeedError(
                "The calendar address returned a web page, not a calendar. "
                "Door Control keeps the last calendar it read."
            )
        self.load_text(raw.decode("utf-8", "replace"))

    def load_text(self, text):
        """Replace the events only with a complete calendar that parses; otherwise keep
        the last good events and raise FeedError."""
        text = (text or "").lstrip("\ufeff")  # a UTF-8 byte order mark (review Q3)
        check_feed(text)
        events = _parse_events(text, self.tz)
        with self._lock:
            self.events = events
            self.last_ok = self.clock()
            self.last_error = None
        self.notifier.recovered("calendar_feed", "The calendar feed can be read again.")

    def start(self):
        if not self.url:
            return
        threading.Thread(target=self._loop, daemon=True, name="calendar").start()

    def refresh_once(self):
        try:
            self.fetch_once()
        except Exception as e:
            # Details (which may include the secret URL) stay in the server log.
            self.last_error = (
                str(e)
                if isinstance(e, FeedError)
                else "The calendar feed could not be read. See the server log."
            )
            log.warning("calendar refresh failed: %s", e)
            self.notifier.outage(
                "calendar_feed",
                "calendar",
                "Calendar problem: %s No calendar window ends early because of it."
                % self.last_error,
                key=("calendar", "feed"),
            )
        self.check_health()

    def _loop(self):
        while not self._stop.is_set():
            self.refresh_once()
            self._stop.wait(self.refresh)

    def state(self, now=None):
        """off (no feed), unknown (not read yet), ok, or stale (no good read for
        stale_minutes)."""
        if not self.url:
            return "off"
        if self.last_ok is None:
            return "unknown"
        now = self.clock() if now is None else now
        return "stale" if now - self.last_ok > self.stale_seconds else "ok"

    def check_health(self):
        st = self.state()
        if st == "stale" and not self._stale_alerted:
            self._stale_alerted = True
            self.notifier.outage(
                "calendar_stale",
                "calendar",
                "Calendar stale: the feed has not been read for over %d minutes. New "
                "calendar unlocks are paused; doors already open for an event close at "
                "that event's end." % round(self.stale_seconds / 60),
                key=("calendar", "stale"),
            )
        elif st == "ok" and self._stale_alerted:
            self._stale_alerted = False
            self.notifier.recovered(
                "calendar_stale", "The calendar is up to date again."
            )
        return st

    # ---- acceptance ----
    def _refuse(self, e, reason, quiet=False):
        uid = e.get("uid") or e.get("title", "")
        key = (uid, reason)
        with self._lock:
            if key in self._reported:
                return
            if len(self._reported) > 10000:
                self._reported.clear()
            self._reported.add(key)
        text = 'Calendar event "%s" (organizer %s) %s. It opens no door.' % (
            safe_title(e.get("title")),
            e.get("organizer") or "none",
            reason,
        )
        if quiet:
            log.debug(text)
            return
        log.warning(text)
        self.notifier.alert("calendar", text, key=("calendar", uid, reason))

    def refusal(self, e, allowed):
        """Why an 'Unlock:' event may not open doors: (reason, quiet) or None."""
        status = e.get("status")
        if status != "CONFIRMED":
            if status == "CANCELLED":
                return "was cancelled", True
            return "is not confirmed (status %s)" % (status or "missing"), False
        if e["problems"]:
            return "was skipped: %s" % "; ".join(e["problems"])[:200], False
        org = e.get("organizer")
        if not org:
            return "has no organizer", False
        if org not in allowed:
            return "has an organizer who is not allowed to unlock doors", False
        ours = self.calendar_email
        if not (ours and org == ours):
            mine = [p for a, p in e["attendees"] if (a == ours if ours else a != org)]
            if any(p in UNANSWERED for p in mine):
                return "is an invitation that was not accepted", False
        try:
            days, dur = event_span(e)
        except ValueError as ex:
            return "was skipped: %s" % ex, False
        # All-day events: one day is the cap; buffers are not counted (review R3).
        hours = (
            days * 24.0
            if days
            else (dur + self.before + self.after).total_seconds() / 3600.0
        )
        if hours > self.max_window_hours:
            return (
                "is longer than the %g-hour limit (one all-day event per day)"
                % self.max_window_hours,
                False,
            )
        if not e["title"].strip()[len(self.prefix) :].strip():
            return "names no door", False
        return None

    def windows(self, first, last):
        """Accepted occurrences overlapping [first, last]: {title, start, end (local
        aware, fold set), door_text, organizer, uid}. Refusals are logged once and
        alerted (deduplicated)."""
        allowed = self.organizers()
        first_u = first.astimezone(UTC) - self.after
        last_u = last.astimezone(UTC) + self.before
        with self._lock:
            events = list(self.events)
        overridden = {
            (e.get("uid"), e["recurrence_id"]) for e in events if "recurrence_id" in e
        }
        out = []
        for e in events:
            title = (e.get("title") or "").strip()
            if not title.casefold().startswith(self.prefix):
                continue
            e = {**e, "title": title}
            try:
                refused = self.refusal(e, allowed)
                if refused:
                    self._refuse(e, *refused)
                    continue
                occs, gaps = expand_event(e, first_u, last_u, overridden)
            except Exception as ex:
                log.warning("calendar: event %r skipped: %r", title[:80], ex)
                self._refuse(e, "could not be read (%s)" % type(ex).__name__)
                continue
            for d in gaps:
                self._refuse(
                    e,
                    "starts at a time that does not exist on %s (clocks spring forward); "
                    "that day is skipped" % d.isoformat(),
                )
            for s, en in occs:
                out.append(
                    {
                        "title": title,
                        "start": (s - self.before).astimezone(self.tz),
                        "end": (en + self.after).astimezone(self.tz),
                        "door_text": title[len(self.prefix) :].strip(),
                        "organizer": e.get("organizer") or "",
                        "uid": e.get("uid") or title,
                    }
                )
        return out
