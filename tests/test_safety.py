"""1.1.0 safety fixes: calendar acceptance and matching, repeat rules, feed health, window
cap, DST, scheduler robustness, health, log retention, backups, startup refusal.

Only mock_unifi.py, fake Telegram (support.Recorder) and fake clocks; no real service.
Run: python3 tests/test_safety.py
"""

import http.client
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from support import (
    JAKE,
    KOLT,
    TZ,
    UTC,
    Clock,
    FakeClock,
    Recorder,
    build,
    put_types,
    sched,
    vcal,
    vevent,
)

import mock_unifi
from doorcontrol import app, backup, ical, web
from doorcontrol.db import DB
from doorcontrol.engine import match_doors
from doorcontrol.ical import Calendar, FeedError
from doorcontrol.unifi import UnifiError

ROOT = os.path.join(os.path.dirname(__file__), "..")


def calendar(ics=None, rec=None, clock=None, **cfg):
    c = Calendar(
        {"ical_url": "https://cal.example/x.ics", "allowed_organizers": [JAKE], **cfg},
        TZ,
        notifier=rec or Recorder(),
        clock=clock or time.time,
    )
    if ics is not None:
        c.load_text(ics)
    return c


def windows(c, y=2027, m=9, d=1, days=40):
    first = datetime(y, m, d, tzinfo=TZ)
    return c.windows(first, first + timedelta(days=days))


def starts(wins):
    return sorted(w["start"].strftime("%Y-%m-%d %H:%M") for w in wins)


class EngineCase(unittest.TestCase):
    """Engine + fake UniFi + calendar + recording notifier."""

    def make(self, ics=None, cfg=None, cal_cfg=None, **kw):
        self.rec = Recorder()
        self.cal = (
            calendar(ics, self.rec, **(cal_cfg or {})) if ics is not None else None
        )
        self.mock, self.srv, self.eng = build(self.cal, self.rec, cfg, **kw)
        self.addCleanup(self.srv.shutdown)
        return self.eng


# ---------------------------------------------------------------- A. acceptance
class CalendarAccept(unittest.TestCase):
    def test_status_tentative_cancelled_missing_make_no_window(self):
        rec = Recorder()
        ics = vcal(
            vevent("Unlock: Office", uid="ok"),
            vevent("Unlock: Office", uid="tent", status="TENTATIVE"),
            vevent("Unlock: Office", uid="canc", status="CANCELLED"),
            vevent("Unlock: Office", uid="none", status=None),
        )
        c = calendar(ics, rec)
        wins = windows(c)
        self.assertEqual([w["uid"] for w in wins], ["ok"])
        texts = " ".join(t for _, t, _ in rec.of("calendar"))
        self.assertIn("status TENTATIVE", texts)
        self.assertIn("status missing", texts)
        self.assertNotIn("cancelled", texts, "deleted events stay quiet")

    def test_organizer_not_allowed_or_missing_refused_logged_alerted_once(self):
        rec = Recorder()
        ics = vcal(
            vevent("Unlock: Office", uid="out", organizer="stranger@example.com"),
            vevent("Unlock: Office", uid="noorg", organizer=None),
        )
        c = calendar(None, rec)
        with self.assertLogs("doorcontrol", "WARNING") as logs:
            for _ in range(3):  # three refreshes
                c.load_text(ics)
                self.assertEqual(windows(c), [])
        out = "\n".join(logs.output)
        self.assertIn("stranger@example.com", out)
        self.assertIn('"Unlock: Office"', out)
        cal_alerts = [t for k, t, _ in rec.calls if k == "calendar"]
        self.assertEqual(len(cal_alerts), 2, cal_alerts)  # once per event
        self.assertEqual(len(rec.texts()), 2)

    def test_allowlist_default_admins(self):
        ics = vcal(vevent("Unlock: Office", organizer=KOLT))
        env = {"GOOGLE_ALLOWED_EMAILS": "Jake@HardinCountyFair.org, " + KOLT}
        with mock.patch.dict(os.environ, env):
            c = Calendar({"ical_url": "https://x/y"}, TZ, notifier=Recorder())
            c.load_text(ics)
            self.assertEqual(len(windows(c)), 1)
        with mock.patch.dict(os.environ, {"GOOGLE_ALLOWED_EMAILS": ""}):
            self.assertEqual(windows(c), [])

    def test_config_allowlist_overrides(self):
        ics = vcal(
            vevent("Unlock: Office", uid="a", organizer=JAKE),
            vevent("Unlock: Office", uid="b", organizer="doors@group.calendar.test"),
        )
        with mock.patch.dict(os.environ, {"GOOGLE_ALLOWED_EMAILS": JAKE}):
            c = calendar(ics, allowed_organizers=["Doors@group.calendar.test"])
            self.assertEqual([w["uid"] for w in windows(c)], ["b"])

    def test_both_empty_no_window_one_startup_alert(self):
        rec = Recorder()
        ics = vcal(vevent("Unlock: Office"))
        with mock.patch.dict(os.environ, {"GOOGLE_ALLOWED_EMAILS": ""}):
            c = calendar(ics, rec, allowed_organizers=[])
            self.assertEqual(windows(c), [])
            cfg = {"ical_url": "https://x/y"}
            self.assertTrue(app.warn_no_organizers(cfg, rec))
            self.assertFalse(app.warn_no_organizers({"ical_url": ""}, rec))
        startup = [t for _, t, k in rec.calls if k == ("calendar", "no-organizers")]
        self.assertEqual(len(startup), 1)
        self.assertIn("nobody is allowed", startup[0])

    def test_invitation_needs_action_or_tentative_refused_even_if_organizer_allowed(
        self,
    ):
        att = lambda who, ps: "ATTENDEE;PARTSTAT=%s:mailto:%s" % (ps, who)
        ics = vcal(
            vevent("Unlock: Office", uid="inv", extra=[att(KOLT, "NEEDS-ACTION")]),
            vevent("Unlock: Office", uid="tent", extra=[att(KOLT, "TENTATIVE")]),
            vevent("Unlock: Office", uid="nops", extra=["ATTENDEE:mailto:" + KOLT]),
            vevent("Unlock: Office", uid="acc", extra=[att(KOLT, "ACCEPTED")]),
        )
        rec = Recorder()
        c = calendar(ics, rec)
        self.assertEqual([w["uid"] for w in windows(c)], ["acc"])
        self.assertTrue(any("invitation" in t for _, t, _ in rec.of("calendar")))
        # With calendar_email: only that attendee counts.
        ours = "doors@group.calendar.test"
        ics = vcal(
            vevent(
                "Unlock: Office",
                uid="ours-ok",
                extra=[att(ours, "ACCEPTED"), att(KOLT, "NEEDS-ACTION")],
            ),
            vevent("Unlock: Office", uid="ours-no", extra=[att(ours, "NEEDS-ACTION")]),
        )
        c = calendar(ics, calendar_email=ours)
        self.assertEqual([w["uid"] for w in windows(c)], ["ours-ok"])

    def test_alarm_attendee_is_not_an_invitation(self):
        ics = vcal(
            vevent(
                "Unlock: Office",
                extra=[
                    "BEGIN:VALARM",
                    "ACTION:EMAIL",
                    "ATTENDEE:mailto:" + KOLT,
                    "END:VALARM",
                ],
            )
        )
        self.assertEqual(len(windows(calendar(ics))), 1)


class AllAndStar(EngineCase):
    def test_all_and_star_open_nothing_and_alert(self):
        ics = vcal(
            vevent("Unlock: ALL", uid="all"),
            vevent("Unlock: *", uid="star"),
            vevent("unlock: all and Office", uid="mix"),
        )
        eng = self.make(ics)
        eng.at(2027, 9, 6, 19)
        with self.assertLogs("doorcontrol", "WARNING"):
            eng.tick()
        self.assertEqual(self.mock.doors["d1"]["door_lock_relay_status"], "lock")
        self.assertEqual(self.mock.doors["d3"]["door_lock_relay_status"], "lock")
        self.assertEqual(
            self.mock.doors["d2"]["door_lock_relay_status"], "unlock", "Office part"
        )
        texts = [t for _, t, _ in self.rec.of("calendar")]
        self.assertEqual(len([t for t in texts if "every door" in t]), 3)


# ---------------------------------------------------------------- A. matching
DOORS = [
    {
        "id": "pba",
        "name": "Pole Barn and Arena",
        "full_name": "Fair - Pole Barn and Arena",
    },
    {"id": "pb", "name": "Pole Barn", "full_name": "Fair - Pole Barn"},
    {"id": "ar", "name": "Arena", "full_name": "Fair - Arena"},
    {"id": "mg", "name": "Main Gate", "full_name": "Fair - Main Gate"},
    {"id": "mo", "name": "Main Office", "full_name": "Fair - Main Office"},
    {"id": "ec", "name": "Event Center", "full_name": "Fair - Event Center"},
]


class MatchDoors(unittest.TestCase):
    def test_full_name_not_split(self):
        self.assertEqual(match_doors("pole barn AND arena", DOORS), ({"pba"}, []))
        self.assertEqual(
            match_doors("Fair - Pole Barn and Arena", DOORS), ({"pba"}, [])
        )

    def test_split_parts_exact_then_single_partial(self):
        self.assertEqual(match_doors("Pole Barn, Arena", DOORS), ({"pb", "ar"}, []))
        self.assertEqual(match_doors("Pole Barn & Gate", DOORS), ({"pb", "mg"}, []))
        self.assertEqual(match_doors("event", DOORS), ({"ec"}, []))

    def test_part_none_or_several_skipped_other_parts_apply(self):
        ids, problems = match_doors("Main, Event Center, Garage", DOORS)
        self.assertEqual(ids, {"ec"})
        self.assertEqual(len(problems), 2)
        self.assertIn("'Main' matches several doors (Main Gate, Main Office)", problems)
        self.assertIn("'Garage' matches no door", problems)
        self.assertEqual(match_doors("   ", DOORS), (set(), ["names no door"]))


class MatchInEngine(EngineCase):
    def test_alert_names_event_and_part(self):
        self.make(vcal(vevent("Unlock: Event Center, Garage")))
        self.eng.at(2027, 9, 6, 19)
        with self.assertLogs("doorcontrol", "WARNING"):
            self.eng.tick()
        self.assertEqual(self.mock.doors["d1"]["door_lock_relay_status"], "unlock")
        (alert,) = [t for _, t, _ in self.rec.of("calendar")]
        self.assertIn('"Unlock: Event Center, Garage"', alert)
        self.assertIn("'Garage' matches no door", alert)
        self.eng.at(2027, 9, 6, 19, 30)
        self.eng.tick()
        self.assertEqual(len(self.rec.of("calendar")), 1, "reported once")


# ---------------------------------------------------------------- A. repeat rules
def rrule_event(rule, uid="r", start="TZID=America/New_York:20270906T180000"):
    return vevent(
        "Unlock: Office",
        uid=uid,
        start=start,
        end=start[:-6] + "210000",
        extra=["RRULE:" + rule],
    )


class Rrule(EngineCase):
    BAD = [
        "FREQ=MONTHLY;BYMONTHDAY=0",
        "FREQ=MONTHLY;BYMONTHDAY=32",
        "FREQ=MONTHLY;BYMONTHDAY=40",
        "FREQ=DAILY;COUNT=0",
        "FREQ=DAILY;COUNT=-1",
        "FREQ=DAILY;COUNT=x",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;INTERVAL=-2",
        "FREQ=DAILY;INTERVAL=x",
        "FREQ=HOURLY",
        "FREQ=SECONDLY",
        "INTERVAL=1",
        "FREQ=DAILY;UNTIL=soon",
        "FREQ=DAILY;BYHOUR=5",
        "FREQ=YEARLY;BYMONTH=9",
    ]

    def test_bad_parts_skipped_and_schedules_still_apply(self):
        events = [rrule_event(r, uid="bad%d" % i) for i, r in enumerate(self.BAD)]
        events.append(
            vevent(
                "Unlock: Office",
                uid="tz",
                start="TZID=Mars/Olympus_Mons:20270906T180000",
                end="TZID=Mars/Olympus_Mons:20270906T210000",
            )
        )
        events.append(vevent("Unlock: Event Center", uid="good"))
        eng = self.make(vcal(*events))
        eng.db.add_schedule(sched(doors=["d3"], start="18:00", end="22:00"))
        eng.at(2027, 9, 6, 19)
        t0 = time.monotonic()
        with self.assertLogs("doorcontrol", "WARNING"):
            eng.tick()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "lock")
        self.assertEqual(self.mock.doors["d1"]["door_lock_relay_status"], "unlock")
        self.assertEqual(self.mock.doors["d3"]["door_lock_relay_status"], "unlock")
        refused = {k[1] for _, _, k in self.rec.of("calendar")}
        self.assertEqual(refused, {"bad%d" % i for i in range(len(self.BAD))} | {"tz"})

    def test_each_bad_rule_bounded(self):
        for rule in self.BAD:
            with self.subTest(rule):
                t0 = time.monotonic()
                c = calendar(vcal(rrule_event(rule)))
                with self.assertLogs("doorcontrol", "WARNING"):
                    self.assertEqual(windows(c), [])
                self.assertLess(time.monotonic() - t0, 2)

    def test_candidate_limit_stops_expansion(self):
        c = calendar(
            vcal(
                rrule_event("FREQ=DAILY", start="TZID=America/New_York:19900101T180000")
            )
        )
        t0 = time.monotonic()
        with self.assertLogs("doorcontrol", "WARNING"):
            self.assertEqual(windows(c), [])
        self.assertLess(time.monotonic() - t0, 2)

    def test_exception_in_one_event_only_drops_it(self):
        real = ical.expand_event

        def boom(e, *a, **k):
            if e.get("uid") == "bad":
                raise ZeroDivisionError("x")
            return real(e, *a, **k)

        c = calendar(
            vcal(
                vevent("Unlock: Office", uid="bad"), vevent("Unlock: Office", uid="ok")
            )
        )
        with mock.patch.object(ical, "expand_event", boom):
            with self.assertLogs("doorcontrol", "WARNING"):
                self.assertEqual([w["uid"] for w in windows(c)], ["ok"])
        # A calendar that fails as a whole never stops schedules.
        eng = self.make(vcal(vevent("Unlock: Office")))
        eng.db.add_schedule(sched(doors=["d3"], start="18:00", end="22:00"))
        eng.at(2027, 9, 6, 19)
        with mock.patch.object(self.cal, "windows", side_effect=RuntimeError("x")):
            with self.assertLogs("doorcontrol", "ERROR"):
                eng.tick()
        self.assertEqual(self.mock.doors["d3"]["door_lock_relay_status"], "unlock")

    def test_monthly_nth_and_last_weekday(self):
        cases = {
            "FREQ=MONTHLY;BYDAY=2TU;COUNT=3": [
                "2027-09-14",
                "2027-10-12",
                "2027-11-09",
            ],
            "FREQ=MONTHLY;BYDAY=1MO;COUNT=3": [
                "2027-10-04",
                "2027-11-01",
                "2027-12-06",
            ],
            "FREQ=MONTHLY;BYDAY=-1FR;COUNT=3": [
                "2027-09-24",
                "2027-10-29",
                "2027-11-26",
            ],
            "FREQ=MONTHLY;BYDAY=TU;BYSETPOS=2;COUNT=2": ["2027-09-14", "2027-10-12"],
            "FREQ=MONTHLY;BYMONTHDAY=6,20;COUNT=3": [
                "2027-09-06",
                "2027-09-20",
                "2027-10-06",
            ],
        }
        for rule, want in cases.items():
            with self.subTest(rule):
                # DTSTART on the first matching day, as Google writes it.
                start = "TZID=America/New_York:%sT180000" % want[0].replace("-", "")
                c = calendar(vcal(rrule_event(rule, start=start)))
                got = [s[:10] for s in starts(windows(c, days=120))]
                self.assertEqual(got, want)

    def test_unsupported_byday_skipped_alerted(self):
        for rule in (
            "FREQ=MONTHLY;BYDAY=5MO",
            "FREQ=MONTHLY;BYDAY=-2FR",
            "FREQ=MONTHLY;BYDAY=MO,TU",
            "FREQ=MONTHLY;BYDAY=MO",
            "FREQ=MONTHLY;BYDAY=TU;BYSETPOS=1,2",
            "FREQ=WEEKLY;BYDAY=1MO",
        ):
            with self.subTest(rule):
                rec = Recorder()
                c = calendar(vcal(rrule_event(rule)), rec)
                with self.assertLogs("doorcontrol", "WARNING"):
                    self.assertEqual(windows(c), [])
                self.assertEqual(len(rec.of("calendar")), 1)

    def test_exdate_value_date_cancels_timed_occurrence(self):
        ev = vevent(
            "Unlock: Office",
            extra=[
                "RRULE:FREQ=DAILY;COUNT=4",
                "EXDATE;VALUE=DATE:20270907",
                "EXDATE;TZID=America/New_York:20270908T180000",
            ],
        )
        got = [s[:10] for s in starts(windows(calendar(vcal(ev))))]
        self.assertEqual(got, ["2027-09-06", "2027-09-09"])


# ---------------------------------------------------------------- A. feed health
BAD_FEEDS = {
    "empty": "",
    "html": "<!doctype html><html><body>Sign in</body></html>",
    "truncated": vcal(vevent("Unlock: Office"))[:-30],
    "no end": vcal(vevent("Unlock: Office")).replace("END:VCALENDAR\n", ""),
    "unbalanced": "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nEND:VCALENDAR\n",
}


class Feed(EngineCase):
    def test_bad_reply_keeps_last_events_sets_error_and_alerts(self):
        good = vcal(vevent("Unlock: Office"))
        eng = self.make(good)
        eng.at(2027, 9, 6, 19)
        eng.tick()
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "unlock")
        last_ok = self.cal.last_ok
        for name, text in BAD_FEEDS.items():
            with self.subTest(name):
                with self.assertRaises(FeedError):
                    self.cal.load_text(text)
                self.cal.fetch_once = lambda t=text: self.cal.load_text(t)
                with self.assertLogs("doorcontrol", "WARNING"):
                    self.cal.refresh_once()
                self.assertIn("not a complete calendar", self.cal.last_error)
                self.assertEqual(self.cal.last_ok, last_ok)
                self.assertEqual(len(self.cal.events), 1)
                eng.at(2027, 9, 6, 20)
                eng.tick()
                self.assertEqual(
                    self.mock.doors["d2"]["door_lock_relay_status"], "unlock"
                )
        feed_alerts = [c for c in self.rec.calls if c[2] == ("calendar", "feed")]
        self.assertEqual(len(feed_alerts), len(BAD_FEEDS))
        self.assertEqual(len([t for t in self.rec.texts() if "complete" in t]), 1)
        # A good read: one "recovered".
        self.cal.load_text(good)
        self.assertEqual(len(self.rec.of("recovered")), 1)
        # An empty but valid calendar is accepted (all events deleted).
        self.cal.load_text(vcal())
        self.assertEqual(self.cal.events, [])

    def test_html_content_type_refused(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = vcal(vevent("Unlock: Office")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = calendar(ical_url="http://127.0.0.1:%d/" % srv.server_address[1])
        with self.assertRaises(FeedError):
            c.fetch_once()
        self.assertIsNone(c.last_ok)

    def test_before_first_fetch_calendar_door_left_and_no_new_window(self):
        eng = self.make(vcal())
        self.cal.last_ok = None  # nothing read yet (startup)
        self.cal.events = ical._parse_events(vcal(vevent("Unlock: Event Center")), TZ)
        self.assertEqual(self.cal.state(), "unknown")
        # A door a calendar event opened before the restart, ending 21:00.
        self.mock.set_by_hand("d2", "keep_unlock")
        ends = datetime(2027, 9, 6, 21, tzinfo=TZ).timestamp()
        eng.db.set_state("d2", "unlock", "reset", "x", source="calendar", ends_at=ends)
        eng.at(2027, 9, 6, 19)
        n = len(self.mock.calls)
        eng.tick()
        self.assertEqual(put_types(self.mock, since=n), [], "nothing opened or closed")
        eng.at(2027, 9, 6, 21, 1)
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2", n), ["reset"], "ends at its end")
        self.assertEqual(self.mock.doors["d1"]["door_lock_relay_status"], "lock")

    def test_stale_one_alert_no_new_windows_open_ends_on_time(self):
        clock = FakeClock()
        self.rec = Recorder()
        self.cal = calendar(
            vcal(
                vevent("Unlock: Office", uid="a"),
                vevent(
                    "Unlock: Event Center",
                    uid="b",
                    start="TZID=America/New_York:20270906T193000",
                    end="TZID=America/New_York:20270906T220000",
                ),
            ),
            self.rec,
            clock=clock,
        )
        self.mock, self.srv, eng = build(self.cal, self.rec)
        self.addCleanup(self.srv.shutdown)
        eng.at(2027, 9, 6, 19)
        eng.tick()
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "unlock")
        clock.advance(61 * 60)
        self.assertEqual(self.cal.state(), "stale")
        eng.at(2027, 9, 6, 20)  # event b has started, but the feed is stale
        eng.tick()
        eng.tick()
        self.assertEqual(self.mock.doors["d1"]["door_lock_relay_status"], "lock")
        stale = [c for c in self.rec.calls if c[2] == ("calendar", "stale")]
        self.assertEqual(len(stale), 1)
        eng.at(2027, 9, 6, 21, 0)  # Office's recorded end
        eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "schedule")
        # Fresh again: one recovered, and b may start.
        self.cal.load_text(self.cal_text if hasattr(self, "cal_text") else vcal())
        self.cal.check_health()
        self.assertTrue(any("up to date" in t for _, t, _ in self.rec.of("recovered")))

    def test_state_records_source_and_ends_at(self):
        eng = self.make(vcal(vevent("Unlock: Event Center")))
        eng.db.add_schedule(sched(doors=["d2"], start="18:00", end="20:00"))
        eng.at(2027, 9, 6, 19)
        eng.tick()
        st1, st2 = eng.db.get_state("d1"), eng.db.get_state("d2")
        self.assertEqual(st1["source"], "calendar")
        self.assertEqual(st2["source"], "schedule")
        self.assertEqual(
            st1["ends_at"], datetime(2027, 9, 6, 21, tzinfo=TZ).timestamp()
        )
        self.assertEqual(
            st2["ends_at"], datetime(2027, 9, 6, 20, tzinfo=TZ).timestamp()
        )

    def test_migration_of_100_door_state(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript(
            """CREATE TABLE door_state(door_id TEXT PRIMARY KEY, applied TEXT NOT NULL,
              end_action TEXT, since REAL, reason TEXT);
            INSERT INTO door_state VALUES('d2','unlock','reset',1.5,'Office');"""
        )
        c.commit()
        c.close()
        db = DB(path)
        st = db.get_state("d2")
        self.assertEqual(
            (st["applied"], st["source"], st["ends_at"], st["hand_alerted"]),
            ("unlock", None, None, 0),
        )
        # 1.0.0's own INSERT still works on the migrated file.
        db.conn.execute(
            "INSERT OR REPLACE INTO door_state(door_id,applied,end_action,since,reason)"
            " VALUES('d1','free',NULL,2.0,NULL)"
        )
        self.assertEqual(db.get_state("d1")["hand_alerted"], 0)
        self.assertEqual(db.kv_get("nothing", "x"), "x")


class AllDayAndCap(unittest.TestCase):
    def allday(self, start, end, uid="ad"):
        return vevent(
            "Unlock: Office",
            uid=uid,
            start="VALUE=DATE:" + start,
            end="VALUE=DATE:" + end,
        )

    def test_all_day_midnight_to_midnight(self):
        (w,) = windows(calendar(vcal(self.allday("20270911", "20270912"))))
        self.assertEqual(w["start"], datetime(2027, 9, 11, tzinfo=TZ))
        self.assertEqual(w["end"], datetime(2027, 9, 12, tzinfo=TZ))
        readme = open(os.path.join(ROOT, "README.md")).read()
        for text in ("midnight to midnight", "looks back"):
            self.assertTrue(text in readme, "README must explain: " + text)

    def test_all_day_on_dst_day_accepted(self):
        (w,) = windows(calendar(vcal(self.allday("20270314", "20270315"))), 2027, 3)
        real = w["end"].astimezone(UTC) - w["start"].astimezone(UTC)
        self.assertEqual(real.total_seconds(), 23 * 3600)

    def test_multiday_all_day_refused(self):
        rec = Recorder()
        c = calendar(vcal(self.allday("20270911", "20270913")), rec)
        with self.assertLogs("doorcontrol", "WARNING"):
            self.assertEqual(windows(c), [])
        self.assertIn("24-hour limit", rec.of("calendar")[0][1])

    def test_over_cap_refused_buffers_counted(self):
        long = vevent(
            "Unlock: Office",
            uid="long",
            start="TZID=America/New_York:20270906T060000",
            end="TZID=America/New_York:20270907T070000",
        )
        c = calendar(vcal(long))
        with self.assertLogs("doorcontrol", "WARNING"):
            self.assertEqual(windows(c), [])
        day = vevent(
            "Unlock: Office",
            uid="23h",
            start="TZID=America/New_York:20270906T060000",
            end="TZID=America/New_York:20270907T050000",
        )
        self.assertEqual(len(windows(calendar(vcal(day)))), 1)
        c = calendar(vcal(day), buffer_before_min=60, buffer_after_min=30)
        with self.assertLogs("doorcontrol", "WARNING"):
            self.assertEqual(windows(c), [], "23 h + 1.5 h of buffers")
        c = calendar(vcal(day), max_window_hours=16)
        with self.assertLogs("doorcontrol", "WARNING"):
            self.assertEqual(windows(c), [])


# ---------------------------------------------------------------- A. DST
class Dst(EngineCase):
    def run_ticks(self, start_utc, end_utc, step=5):
        t = start_utc
        while t <= end_utc:
            self.eng.at_utc(t)
            self.eng.tick()
            t += timedelta(minutes=step)

    def test_fall_back_0130_0145_once(self):
        eng = self.make()
        eng.db.add_schedule(sched(days=[6], start="01:30", end="01:45"))  # Sunday
        # 2027-11-07: 01:00 EDT (05:00Z) ... 02:30 EST (07:30Z); 01:xx happens twice.
        self.run_ticks(
            datetime(2027, 11, 7, 4, 55, tzinfo=UTC),
            datetime(2027, 11, 7, 7, 30, tzinfo=UTC),
        )
        self.assertEqual(put_types(self.mock, "d2"), ["keep_unlock", "reset"])

    def test_end_0200_no_extra_hour(self):
        eng = self.make()
        eng.db.add_schedule(sched(days=[6], start="00:30", end="02:00"))
        eng.at_utc(datetime(2027, 11, 7, 5, 30, tzinfo=UTC))  # 01:30 EST
        eng.tick()
        eng.at_utc(datetime(2027, 11, 7, 6, 59, tzinfo=UTC))  # 01:59 EST
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2"), ["keep_unlock"])
        eng.at_utc(datetime(2027, 11, 7, 7, 0, tzinfo=UTC))  # 02:00 EST
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2"), ["keep_unlock", "reset"])

    def test_fall_back_calendar_event_once(self):
        ev = vevent(
            "Unlock: Office",
            start="TZID=America/New_York:20271107T013000",
            end="TZID=America/New_York:20271107T014500",
        )
        self.make(vcal(ev))
        self.run_ticks(
            datetime(2027, 11, 7, 4, 55, tzinfo=UTC),
            datetime(2027, 11, 7, 7, 30, tzinfo=UTC),
        )
        self.assertEqual(put_types(self.mock, "d2"), ["keep_unlock", "reset"])

    def test_spring_forward_start_in_gap_refused_alerted_never_twice(self):
        eng = self.make()
        sid = eng.db.add_schedule(sched(days=[6], start="02:30", end="04:00"))
        with self.assertLogs("doorcontrol", "WARNING"):
            self.run_ticks(
                datetime(2027, 3, 14, 5, 0, tzinfo=UTC),
                datetime(2027, 3, 14, 9, 0, tzinfo=UTC),
            )
        self.assertEqual(put_types(self.mock, "d2"), [])
        alerts = [t for k, t, key in self.rec.calls if key and key[0] == "schedule"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("2027-03-14", alerts[0])
        self.assertEqual(sid, 1)
        # The next Sunday it runs normally.
        self.run_ticks(
            datetime(2027, 3, 21, 6, 25, tzinfo=UTC),
            datetime(2027, 3, 21, 6, 35, tzinfo=UTC),
        )
        self.assertEqual(put_types(self.mock, "d2"), ["keep_unlock"])

    def test_spring_forward_end_in_gap_moved_to_0300(self):
        eng = self.make()
        eng.db.add_schedule(sched(days=[6], start="01:00", end="02:30"))
        eng.at_utc(datetime(2027, 3, 14, 6, 30, tzinfo=UTC))  # 01:30 EST
        eng.tick()
        eng.at_utc(datetime(2027, 3, 14, 6, 59, tzinfo=UTC))  # 01:59 EST
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2"), ["keep_unlock"])
        eng.at_utc(datetime(2027, 3, 14, 7, 0, tzinfo=UTC))  # 03:00 EDT
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2"), ["keep_unlock", "reset"])

    def test_spring_forward_calendar_event_in_gap_refused(self):
        single = vevent(
            "Unlock: Office",
            uid="single",
            start="TZID=America/New_York:20270314T023000",
            end="TZID=America/New_York:20270314T040000",
        )
        weekly = vevent(
            "Unlock: Event Center",
            uid="weekly",
            start="TZID=America/New_York:20270307T023000",
            end="TZID=America/New_York:20270307T040000",
            extra=["RRULE:FREQ=WEEKLY;COUNT=3"],
        )
        self.make(vcal(single, weekly))
        with self.assertLogs("doorcontrol", "WARNING"):
            self.run_ticks(
                datetime(2027, 3, 14, 5, 0, tzinfo=UTC),
                datetime(2027, 3, 14, 9, 0, tzinfo=UTC),
            )
        self.assertEqual(put_types(self.mock), [])
        refused = [k for _, _, k in self.rec.of("calendar")]
        self.assertEqual(len(refused), 2, refused)
        got = starts(windows(self.cal, 2027, 3, 1, 30))
        self.assertEqual(got, ["2027-03-07 02:30", "2027-03-21 02:30"])


class Attribution(EngineCase):
    def test_calendar_unlock_logged_as_calendar_organizer_with_title(self):
        self.make(vcal(vevent("Unlock: Office")))
        self.eng.at(2027, 9, 6, 19)
        self.eng.tick()
        row = self.eng.db.recent_log(1)[0]
        self.assertEqual(row["user"], "calendar:" + JAKE)
        self.assertEqual(row["source"], "calendar: Unlock: Office")
        self.assertIn('"Unlock: Office"', row["detail"])
        (status,) = self.rec.of("status")
        self.assertIn("calendar:" + JAKE, status[1])
        self.assertIn('Event "Unlock: Office" (UID u1).', status[1])  # review S12
        self.assertIn("until Mon 9:00 PM", status[1])


# ---------------------------------------------------------------- B. scheduler
class Scheduler(EngineCase):
    def test_two_ticks_never_overlap(self):
        eng = self.make()
        eng.db.add_schedule(sched())
        eng.at(2027, 9, 6, 9)
        eng.doors()  # warm the door cache
        inside, most = [0], [0]
        real = eng._tick

        def counted():
            inside[0] += 1
            most[0] = max(most[0], inside[0])
            try:
                real()
            finally:
                inside[0] -= 1

        eng._tick = counted
        self.mock.hang = 0.4
        t = threading.Thread(target=eng.tick)
        t.start()
        time.sleep(0.1)
        eng.request_tick()  # the web's tick_soon while a tick runs
        t.join(10)
        self.mock.hang = 0
        self.assertEqual(most[0], 1, "never two ticks at once")
        self.assertEqual(put_types(self.mock, "d2"), ["keep_unlock"], "one UniFi call")
        n = len(self.mock.calls)
        eng.request_tick()  # nothing running: runs now
        self.assertEqual(put_types(self.mock, since=n), [])

    def test_overlap_keep_lock_started_later_wins_at_end(self):
        eng = self.make()
        eng.db.add_schedule(sched(start="08:00", end="12:00"))
        eng.db.add_schedule(sched(start="11:00", end="12:00", end_action="keep_lock"))
        eng.at(2027, 9, 6, 9)
        eng.tick()
        eng.at(2027, 9, 6, 11, 30)
        eng.tick()
        eng.at(2027, 9, 6, 12, 1)
        eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "keep_lock")

    def test_keep_lock_that_ended_earlier_does_not_count(self):
        eng = self.make()
        eng.db.add_schedule(sched(start="08:00", end="12:00"))
        eng.db.add_schedule(sched(start="09:00", end="10:00", end_action="keep_lock"))
        eng.at(2027, 9, 6, 9, 30)
        eng.tick()
        eng.at(2027, 9, 6, 12, 1)
        eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "schedule")

    def test_end_action_same_after_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        mock1, srv1, eng1 = build(db_path=path)
        self.addCleanup(srv1.shutdown)
        eng1.db.add_schedule(sched(start="08:00", end="12:00"))
        eng1.db.add_schedule(sched(start="11:00", end="12:00", end_action="keep_lock"))
        eng1.at(2027, 9, 6, 9)
        eng1.tick()
        # Restart: a new engine on the same database and console.
        eng2 = Clock(
            {"timezone": "America/New_York"}, DB(path), eng1.client, None, Recorder()
        )
        eng2.at(2027, 9, 6, 12, 5)
        eng2.tick()
        self.assertEqual(mock1.rules["d2"]["type"], "keep_lock")

    def test_non_unifi_error_on_one_door_others_processed_and_alerted(self):
        eng = self.make()
        eng.db.add_schedule(sched(doors=["*"]))
        real = eng.db.get_state

        def flaky(door_id):
            if door_id == "d1":
                raise KeyError("boom")
            return real(door_id)

        eng.db.get_state = flaky
        eng.at(2027, 9, 6, 9)
        with self.assertLogs("doorcontrol", "ERROR"):
            eng.tick()
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "unlock")
        self.assertEqual(self.mock.doors["d3"]["door_lock_relay_status"], "unlock")
        (err,) = self.rec.of("scheduler")
        self.assertIn("Event Center", err[1])
        self.assertIsNotNone(eng.last_tick)

    def test_hand_change_left_alone_logged_once_alerted_once(self):
        eng = self.make()
        eng.db.add_schedule(sched())
        eng.at(2027, 9, 6, 9)
        eng.tick()
        self.mock.set_by_hand("d2", "lock_now")
        n = len(self.mock.calls)
        for minute in (1, 6, 12, 20, 40):
            eng.at(2027, 9, 6, 9, minute)
            eng.tick()
        eng.at(2027, 9, 6, 17, 1)
        eng.tick()
        self.assertEqual(put_types(self.mock, since=n), [], "never touched")
        rows = [r for r in eng.db.recent_log(50) if r["action"] == "changed by hand"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.rec.of("hand_change")), 1)
        self.assertIn("lock_now", self.rec.of("hand_change")[0][1])
        self.assertEqual(eng.db.get_state("d2")["applied"], "free")
        gets = [c for c in self.mock.calls[n:] if c[0] == "GET" and "lock_rule" in c[1]]
        self.assertLessEqual(len(gets), 2, "checked every 5 minutes, then never")

    def test_hand_change_found_at_end_alerted(self):
        eng = self.make()
        eng.db.add_schedule(sched())
        eng.at(2027, 9, 6, 9)
        eng.tick()
        self.mock.set_by_hand("d2", "keep_lock")
        eng.at(2027, 9, 6, 17, 0)
        eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "keep_lock")
        self.assertEqual(len(self.rec.of("hand_change")), 1)


class Outage(EngineCase):
    def test_unifi_down_one_row_per_door_action_plus_recovery(self):
        eng = self.make()
        eng.db.add_schedule(sched(doors=["d2", "d3"]))
        eng.at(2027, 9, 6, 7)
        eng.tick()  # warms the door cache
        self.mock.fail_paths = [("PUT", r".*/lock_rule")]
        with self.assertLogs("doorcontrol", "WARNING"):
            for minute in range(0, 10):
                eng.at(2027, 9, 6, 8, minute)
                eng.tick()
        rows = [r for r in eng.db.recent_log(100) if not r["ok"]]
        self.assertEqual(
            sorted((r["door"], r["action"]) for r in rows),
            [
                ("Fairgrounds - Main Gate", "keep_unlock"),
                ("Fairgrounds - Office", "keep_unlock"),
            ],
        )
        self.assertEqual(len(self.rec.of("door_error")), 2, "one per door, after 2")
        self.mock.fail_paths = []
        eng.at(2027, 9, 6, 8, 11)
        eng.tick()
        ok = [r for r in eng.db.recent_log(2)]
        self.assertTrue(
            all("worked again after 10 failed tries" in r["detail"] for r in ok)
        )
        self.assertEqual(len(self.rec.of("recovered")), 2)

    def test_door_list_outage_counts_as_completed_tick(self):
        eng = self.make()
        self.mock.fail = True
        eng.at(2027, 9, 6, 8)
        for _ in range(3):
            with self.assertLogs("doorcontrol", "WARNING"):
                with self.assertRaises(UnifiError):
                    eng.tick()
        self.assertIsNotNone(eng.last_tick)
        outage = [c for c in self.rec.calls if c[2] == ("unifi", "down")]
        self.assertEqual(len(outage), 2)  # 2nd and 3rd failure asked; dedupe sends 1
        self.assertEqual(len([t for t in self.rec.texts() if "UniFi problem" in t]), 1)
        self.mock.fail = False
        eng._cache = (0.0, [])
        eng.tick()
        self.assertTrue(
            any("reached again" in t for _, t, _ in self.rec.of("recovered"))
        )

    def test_500_on_end_lock_state_stays_unlock_and_retried(self):
        eng = self.make()
        eng.db.add_schedule(sched())
        eng.at(2027, 9, 6, 9)
        eng.tick()
        self.mock.fail_paths = [("PUT", r".*/d2/lock_rule")]
        eng.at(2027, 9, 6, 17, 0)
        with self.assertLogs("doorcontrol", "WARNING"):
            eng.tick()
            eng.at(2027, 9, 6, 17, 1)
            eng.tick()
        self.assertEqual(eng.db.get_state("d2")["applied"], "unlock")
        self.assertEqual(self.mock.rules["d2"]["type"], "keep_unlock")
        self.mock.fail_paths = []
        eng.at(2027, 9, 6, 17, 2)
        eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "schedule")
        self.assertEqual(eng.db.get_state("d2")["applied"], "free")

    def test_timeout_end_lock_retried(self):
        eng = self.make(timeout=0.5)
        eng.db.add_schedule(sched(doors=["d1", "d2"]))
        eng.at(2027, 9, 6, 9)
        eng.tick()
        self.mock.hang = 1.0
        eng.at(2027, 9, 6, 17, 0)
        t0 = time.monotonic()
        with self.assertLogs("doorcontrol", "WARNING"):
            eng.tick()
        self.assertLess(time.monotonic() - t0, 2.5, "rest of the tick skipped")
        self.assertEqual(eng.db.get_state("d2")["applied"], "unlock")
        self.mock.hang = 0
        time.sleep(1.1)  # let the mock finish the slow answer
        eng.at(2027, 9, 6, 17, 1)
        eng.tick()
        self.assertEqual(eng.db.get_state("d1")["applied"], "free")
        self.assertEqual(eng.db.get_state("d2")["applied"], "free")


class Watchdog(EngineCase):
    def test_stalled_gives_503_and_one_alert_then_recovered(self):
        eng = self.make()
        release = threading.Event()
        eng._tick = lambda: release.wait(20)  # a tick that hangs
        eng.start(interval=1)
        self.addCleanup(eng.stop)
        self.addCleanup(release.set)
        time.sleep(3.5)
        self.assertFalse(eng.check_health())
        self.assertFalse(eng.check_health())
        stalled = [c for c in self.rec.calls if c[2] == ("scheduler", "stalled")]
        self.assertEqual(len(stalled), 1)
        st = WebStack(eng)
        self.addCleanup(st.close)
        r = st.health()
        self.assertEqual((r[0], r[1]), (503, {"ok": False, "version": web.VERSION}))
        eng._tick = lambda: None
        release.set()
        for _ in range(50):
            if eng.check_health():
                break
            time.sleep(0.1)
        self.assertTrue(eng.healthy())
        self.assertEqual(st.health()[0], 200)
        self.assertTrue(
            any("running again" in t for _, t, _ in self.rec.of("recovered"))
        )

    def test_dead_thread_is_unhealthy(self):
        eng = self.make()
        eng._thread = threading.Thread(target=lambda: None)
        eng._thread.start()
        eng._thread.join()
        eng._started = eng.last_tick = eng.clock()
        self.assertFalse(eng.healthy())

    def test_never_started_is_healthy(self):
        eng = self.make()
        self.assertTrue(eng.healthy())


class WebStack:
    def __init__(self, eng):
        env = {
            "PUBLIC_HOSTS": "app.test",
            "ALLOWED_HOSTS": "",
            "TRUSTED_PROXIES": "127.0.0.1/32",
        }
        with mock.patch.dict(os.environ, env):
            self.web = web.make_server(eng, tempfile.mkdtemp(), "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def health(self, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.web.server_address[1])
        c.request("GET", "/health", headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, (json.loads(body) if body else None)

    def close(self):
        self.web.shutdown()
        self.web.server_close()


class Health(EngineCase):
    def test_health_loopback_only_no_details(self):
        eng = self.make()
        st = WebStack(eng)
        self.addCleanup(st.close)
        self.assertEqual(st.health(), (200, {"ok": True, "version": web.VERSION}))
        self.assertEqual(st.health({"CF-Connecting-IP": "1.2.3.4"})[0], 404)
        docker = open(os.path.join(ROOT, "Dockerfile")).read()
        self.assertIn("HEALTHCHECK", docker)
        self.assertIn("urlopen('http://127.0.0.1:8480/health'", docker)


class LogRetention(unittest.TestCase):
    def test_signins_do_not_push_out_door_rows(self):
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
        db.log("Office", "lock", "web", True, "locked now", user=JAKE)
        for i in range(6000):
            db.log("", "sign-in", "web", True, "signed in", user=JAKE, kind="signin")
        db.prune(90)
        kinds = dict(
            db.conn.execute("SELECT kind, COUNT(*) FROM log GROUP BY kind").fetchall()
        )
        self.assertEqual(kinds, {"door": 1, "signin": 5000})

    def test_prune_by_age_90_days(self):
        db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
        now = time.time()
        for age in (100, 91, 89, 1):
            db.log("Office", "lock", "web", True, str(age))
            db.conn.execute(
                "UPDATE log SET ts=? WHERE id=(SELECT MAX(id) FROM log)",
                (now - age * 86400,),
            )
        db.prune(90, now=now)
        self.assertEqual([r["detail"] for r in db.recent_log(10)], ["1", "89"])


# ---------------------------------------------------------------- F. backups
class Backup(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        db = DB(os.path.join(self.data, "doorcontrol.db"))
        db.add_schedule(sched())
        db.conn.close()
        for name, text in (
            ("config.json", '{"unifi": {"token": "t"}}'),
            ("accounts.json", "{}"),
            ("session-secret", "s" * 32),
        ):
            with open(os.path.join(self.data, name), "w") as f:
                f.write(text)

    def test_daily_file_600_14_kept_restores_working_db(self):
        t0 = 2_000_000_000
        paths = [backup.run(self.data, now=t0) for _ in range(3)]
        base = paths[0].name[: -len(".tar.gz")]
        self.assertRegex(base, r"^door-control-\d{8}-\d{6}$")
        self.assertEqual(
            [p.name for p in paths[1:]],
            [base + "-2.tar.gz", base + "-3.tar.gz"],
            "same second: collision-safe names",
        )
        self.assertEqual(len({p.name for p in paths}), 3, "no name is reused")
        folder = backup.backup_dir(self.data)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        for p in paths:
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        for i in range(15):
            p = backup.run(self.data, now=t0 + 60 * (i + 1))
        files = backup.backups(self.data)
        self.assertEqual(len(files), 14)
        self.assertEqual(files[0], p, "newest kept")
        out = tempfile.mkdtemp()
        with tarfile.open(files[0]) as tar:
            self.assertEqual(
                sorted(tar.getnames()),
                ["accounts.json", "config.json", "doorcontrol.db", "session-secret"],
            )
            tar.extractall(out, filter="data")
        c = sqlite3.connect(os.path.join(out, "doorcontrol.db"))
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        db = DB(os.path.join(out, "doorcontrol.db"))
        self.assertEqual(db.schedules()[0]["name"], "Office hours")
        self.assertEqual(list(os.listdir(folder)).count("tmp"), 0)
        self.assertTrue(all(n.endswith(".tar.gz") for n in os.listdir(folder)))

    def test_manage_backup(self):
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "manage.py"), "backup"],
            capture_output=True,
            text=True,
            env={**os.environ, "DATA_DIR": self.data},
            cwd=ROOT,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        path = r.stdout.strip()
        self.assertTrue(path.endswith(".tar.gz") and os.path.isfile(path))
        empty = tempfile.mkdtemp()
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "manage.py"), "backup"],
            capture_output=True,
            text=True,
            env={**os.environ, "DATA_DIR": empty},
            cwd=ROOT,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Backup failed", r.stderr)

    def test_failed_backup_alerted_once(self):
        rec = Recorder()
        stop = backup.start(tempfile.mkdtemp(), None, rec, every=0.05)
        self.addCleanup(stop.set)
        with self.assertLogs("doorcontrol", "WARNING"):
            time.sleep(0.4)
        stop.set()
        asked = [c for c in rec.calls if c[2] == ("backup", "failed")]
        self.assertGreaterEqual(len(asked), 2)
        self.assertEqual(len([t for t in rec.texts() if "Backup problem" in t]), 1)


# ---------------------------------------------------------------- E. startup refusal
class Startup(unittest.TestCase):
    def test_check_unifi(self):
        pin = {"host": "https://192.168.1.1:12445", "cert_sha256": "ab" * 32}
        self.assertEqual(app.check_unifi(pin, {}), "pinned")
        self.assertEqual(
            app.check_unifi({"host": pin["host"], "verify_tls": True}, {}), "verified"
        )
        for bad in (
            {"host": "http://192.168.1.1:12445", "cert_sha256": "ab" * 32},
            {"host": "https://192.168.1.1:12445"},
            {"host": "https://192.168.1.1:12445", "verify_tls": False},
            {"host": "https://192.168.1.1:12445", "verify_tls": "yes"},
            {"host": ""},
        ):
            with self.subTest(bad):
                with self.assertRaises(SystemExit) as e:
                    app.check_unifi(bad, {})
                self.assertIn("did not start", str(e.exception.code))

    def test_dev_escape_only_loopback_http(self):
        dev = {"DOORCONTROL_DEV_ALLOW_HTTP": "1"}
        for host in ("http://127.0.0.1:12445", "http://localhost:12445"):
            self.assertEqual(app.check_unifi({"host": host}, dev), "development")
        for host in ("http://192.168.1.1:12445", "http://127.0.0.1.evil.test"):
            with self.assertRaises(SystemExit):
                app.check_unifi({"host": host}, dev)
        with self.assertRaises(SystemExit):
            app.check_unifi({"host": "http://127.0.0.1:12445"}, {})

    def test_http_host_refused_no_request_sent(self):
        m = mock_unifi.Mock()
        srv = mock_unifi.make_server(m)
        self.addCleanup(srv.shutdown)
        data = tempfile.mkdtemp()
        for host, extra in (
            ("http://127.0.0.1:%d" % srv.server_address[1], {}),
            ("https://127.0.0.1:%d" % srv.server_address[1], {"verify_tls": False}),
        ):
            cfg = {"unifi": {"host": host, "token": mock_unifi.TOKEN, **extra}}
            path = os.path.join(data, "config.json")
            with open(path, "w") as f:
                json.dump(cfg, f)
            env = {"DATA_DIR": data, "DOORCONTROL_DEV_ALLOW_HTTP": ""}
            with mock.patch.dict(os.environ, env):
                with self.assertRaises(SystemExit):
                    app.main([path])
        self.assertEqual(m.calls, [], "nothing (and no token) sent")
        self.assertFalse(os.path.exists(os.path.join(data, "doorcontrol.db")))


# ---------------------------------------------------------------- F. repo / release
def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def section_block(text, heading):
    """The first ```bash block after a heading."""
    return text.split(heading, 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]


def bash_ok(block):
    r = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True)
    return r.returncode == 0, r.stderr


class Repo(unittest.TestCase):
    def test_version_agrees(self):
        v = read("VERSION").strip()
        self.assertEqual(v, "1.1.0")
        self.assertIn("image: door-control:%s\n" % v, read("compose.yaml"))
        self.assertEqual(
            read("README.md").splitlines()[0], "# Fairgrounds Door Control " + v
        )
        self.assertTrue(read("CHANGELOG.md").split("\n## ")[1].startswith(v + " "))
        claude = read("CLAUDE.md")
        self.assertIn("Current version: **%s**" % v, claude)
        self.assertIn("- %s (" % v, claude.split("## History")[1])

    def test_compose_log_rotation_and_readme_privacy(self):
        compose = read("compose.yaml")
        logging_part = compose.split("logging:", 1)[1]
        self.assertIn("driver: json-file", logging_part)
        self.assertRegex(logging_part, r'max-size: "\d+m"')
        self.assertRegex(logging_part, r'max-file: "\d+"')
        readme = read("README.md")
        self.assertIn("sign-in emails and IP addresses", readme)

    def test_dockerfile_tz(self):
        self.assertRegex(read("Dockerfile"), r"(?m)^ENV TZ=America/New_York$")

    def test_sudo_note_for_first_move(self):
        for name in ("README.md", "CLAUDE.md"):
            with self.subTest(name):
                self.assertIn("sudo password", read(name))
                self.assertIn("sudo -v", read(name))

    def test_deploy_block_order(self):
        block = section_block(read("CLAUDE.md"), "### Deploy\n")
        self.assertEqual(bash_ok(block), (True, ""))
        self.assertTrue(block.startswith("(set -eE\n") and block.rstrip().endswith(")"))
        steps = [
            "docker version",
            "systemctl is-active",
            "previous-commit",
            "s.backup(d)",
            'test -f "$BK/config.json"',
            "openssl s_client",
            'c["unifi"]["cert_sha256"]',
            "git pull --ff-only",
            "docker compose build",
            "trap restore_prev ERR",
            "docker compose up -d",
            "Health.Status",
            "trap - ERR",
        ]
        at = 0
        for x in steps:  # each step found after the previous one
            self.assertIn(x, block[at:], "step out of order: " + x)
            at = block.index(x, at) + len(x)
        main = block.split("trap restore_prev ERR", 1)[0]
        self.assertNotIn("docker compose up -d\nS=", main, "start only after the trap")
        self.assertIn('git checkout -q "$PREV"', block.split("restore_prev()")[1])
        self.assertNotIn("--build", block, "builds before anything restarts")
        self.assertNotIn("door-control:1.", block, "no image tag in the block")

    def test_rollback_block_checks_before_stop_and_restores_files(self):
        block = section_block(
            read("CLAUDE.md"), "### Rollback to the previous Docker version"
        )
        self.assertEqual(bash_ok(block), (True, ""))
        before, after = block.split("docker compose stop\nput_back", 1)
        for check in (
            'if [ -e "$BK/restored" ]',
            "systemctl is-active",
            "REPLACE NEWER DATA",
            "pre-rollback-",
            "trap restore_new ERR",
        ):
            self.assertIn(check, before)
        for f in ("doorcontrol.db", "config.json", "accounts.json", "session-secret"):
            self.assertIn(f, block.split("put_back() {")[1].split("}")[0])
        self.assertIn('date > "$BK/restored"', after)
        self.assertIn('put_back "$PRE"', block.split("restore_new()")[1])

    def test_rollback_newer_data_check_runs(self):
        snippet = re.search(r"app python -c '([^']*)'\)", read("CLAUDE.md")).group(1)
        d = tempfile.mkdtemp()
        live, bk = os.path.join(d, "live.db"), os.path.join(d, "bk.db")
        for p in (live, bk):
            db = DB(p)
            db.add_schedule(sched())
            db.conn.close()
        code = snippet.replace("/data/doorcontrol.db", live).replace(
            "/bk/doorcontrol.db", bk
        )
        run = lambda: subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(run(), "same")
        db = DB(live)
        db.log("Office", "lock", "web", True, "locked now")
        db.conn.close()
        self.assertEqual(run(), "newer")

    def test_config_example_has_new_keys(self):
        cfg = json.loads(read("config.example.json"))
        self.assertIn("cert_sha256", cfg["unifi"])
        # Review R2: an empty list would count as set and switch off the admin fallback.
        self.assertNotIn("allowed_organizers", cfg["calendar"])
        for k in (
            "calendar_email",
            "stale_minutes",
            "max_window_hours",
        ):
            self.assertIn(k, cfg["calendar"])
        for k in ("max_commands_per_minute", "max_age_seconds"):
            self.assertIn(k, cfg["telegram"])
        from doorcontrol.notify import KINDS

        self.assertEqual(set(cfg["alerts"]["kinds"]), set(KINDS))
        self.assertEqual(cfg["log_days"], 90)
        self.assertEqual(cfg["hand_check_minutes"], 5)

    def test_no_dev_escape_in_compose(self):
        self.assertNotIn("DOORCONTROL_DEV_ALLOW_HTTP", read("compose.yaml"))
        self.assertNotIn("DOORCONTROL_DEV_ALLOW_HTTP", read("Dockerfile"))


# ---------------------------------------------------------------- review round 1
def read_rows(eng, action):
    return [r for r in eng.db.recent_log(200) if r["action"] == action]


class OverlapHold(unittest.TestCase):
    """Review R1: schedule 08-17 and calendar event 16-20 on the same door."""

    EVENT = vevent(
        "Unlock: Event Center",
        uid="ev",
        start="TZID=America/New_York:20270906T160000",
        end="TZID=America/New_York:20270906T200000",
    )

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        self.clock = FakeClock()
        self.rec = Recorder()
        self.cal = calendar(vcal(self.EVENT), self.rec, clock=self.clock)
        self.mock, srv, self.eng = build(self.cal, self.rec, db_path=self.path)
        self.addCleanup(srv.shutdown)
        self.eng.db.add_schedule(sched(doors=["d1"], start="08:00", end="17:00"))

    def tick_at(self, eng, h, m=0):
        eng.at(2027, 9, 6, h, m)
        eng.tick()

    def test_stale_feed_keeps_event_end(self):
        self.tick_at(self.eng, 16, 30)
        st = self.eng.db.get_state("d1")
        self.assertEqual(st["source"], "schedule")
        self.assertEqual(
            st["cal_ends_at"], datetime(2027, 9, 6, 20, tzinfo=TZ).timestamp()
        )
        self.clock.advance(61 * 60)
        self.assertEqual(self.cal.state(), "stale")
        n = len(self.mock.calls)
        for h, m in ((16, 45), (17, 1), (18, 0), (19, 59)):
            self.tick_at(self.eng, h, m)
            self.assertEqual(
                put_types(self.mock, "d1", n), [], "held at %d:%02d" % (h, m)
            )
        self.assertEqual(
            self.eng.db.get_state("d1")["ends_at"],
            datetime(2027, 9, 6, 20, tzinfo=TZ).timestamp(),
            "stored end never lowered",
        )
        self.tick_at(self.eng, 20, 1)
        self.assertEqual(put_types(self.mock, "d1", n), ["reset"])

    def test_restart_before_first_fetch_keeps_event_end(self):
        self.tick_at(self.eng, 16, 30)
        # Restart: new engine on the same database, calendar not read yet.
        rec2 = Recorder()
        cal2 = calendar(None, rec2)
        self.assertEqual(cal2.state(), "unknown")
        mock2, srv2, eng2 = build(cal2, rec2, db_path=self.path)
        self.addCleanup(srv2.shutdown)
        mock2.set_by_hand("d1", "keep_unlock")
        for h, m in ((17, 1), (19, 30)):
            self.tick_at(eng2, h, m)
        self.assertEqual(put_types(mock2, "d1"), [], "not reset at the schedule's end")
        self.tick_at(eng2, 20, 1)
        self.assertEqual(put_types(mock2, "d1"), ["reset"])

    def test_trusted_calendar_without_the_event_ends_with_the_schedule(self):
        self.tick_at(self.eng, 16, 30)
        self.cal.load_text(vcal())  # event removed, feed fresh
        n = len(self.mock.calls)
        self.tick_at(self.eng, 17, 1)
        self.assertEqual(put_types(self.mock, "d1", n), ["reset"])

    def test_calendar_joining_a_schedule_hold_is_alerted_with_uid(self):
        self.tick_at(self.eng, 16, 30)
        opened = [t for _, t, _ in self.rec.of("status") if "UID ev" in t]
        self.assertEqual(len(opened), 1)
        self.assertIn("Event Center", opened[0])
        self.tick_at(self.eng, 16, 31)
        self.assertEqual(
            len([t for _, t, _ in self.rec.of("status") if "UID ev" in t]), 1
        )


class UnclearRule(EngineCase):
    """Review Q1/Q2: only a real, different lock rule is a change by hand."""

    def held(self):
        eng = self.make(vcal())
        eng.db.add_schedule(sched(doors=["d2"], start="18:00", end="20:00"))
        eng.at(2027, 9, 6, 19)
        eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "keep_unlock")
        return eng

    def test_end_window_empty_answers_retry_and_alert(self):
        eng = self.held()
        n = len(self.mock.calls)
        for i, bad in enumerate(({}, None, {"type": ""}, {"type": None}, {"x": 1})):
            self.mock.rules["d2"] = bad
            eng.at(2027, 9, 6, 20, 1 + i)
            eng.tick()
            self.assertEqual(put_types(self.mock, "d2", n), [], "door left alone")
            self.assertEqual(eng.db.get_state("d2")["applied"], "unlock", "retried")
        self.assertEqual(self.rec.of("hand_change"), [])
        (alert,) = [
            c for c in self.rec.of("door_error") if c[2] == ("door", "d2", "read")
        ]
        self.assertIn("could not read the door", alert[1])
        rows = read_rows(eng, "read door")
        self.assertEqual(len(rows), 1, "one row per outage")
        self.assertFalse(rows[0]["ok"])
        self.mock.rules["d2"] = {"type": "keep_unlock", "ended_time": 0}
        eng.at(2027, 9, 6, 20, 10)
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2", n), ["reset"])
        self.assertEqual(eng.db.get_state("d2")["applied"], "free")
        self.assertEqual(len(read_rows(eng, "read door")), 2)
        self.assertTrue(
            any("can be read again" in t for _, t, _ in self.rec.of("recovered"))
        )

    def test_end_window_real_other_rule_is_hand_change(self):
        eng = self.held()
        self.mock.set_by_hand("d2", "keep_lock")
        n = len(self.mock.calls)
        eng.at(2027, 9, 6, 20, 1)
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2", n), [])
        self.assertEqual(len(self.rec.of("hand_change")), 1)
        self.assertEqual(eng.db.get_state("d2")["applied"], "free")

    def test_hand_check_empty_answer_retried_not_hand_change(self):
        eng = self.held()
        self.mock.rules["d2"] = {}
        eng.at(2027, 9, 6, 19, 6)  # hand check due
        eng.tick()
        gets = lambda: len(
            [c for c in self.mock.calls if c[0] == "GET" and "/d2/lock_rule" in c[1]]
        )
        g = gets()
        eng.at(2027, 9, 6, 19, 7)  # inside the 5 minutes: asked again anyway
        eng.tick()
        self.assertEqual(gets(), g + 1)
        self.assertEqual(self.rec.of("hand_change"), [])
        self.assertFalse(eng.db.get_state("d2")["hand_alerted"])
        self.assertTrue(
            any("could not read the door" in t for _, t, _ in self.rec.of("door_error"))
        )
        self.mock.set_by_hand("d2", "lock_now")
        eng.at(2027, 9, 6, 19, 8)
        eng.tick()
        self.assertEqual(len(self.rec.of("hand_change")), 1)
        self.assertTrue(eng.db.get_state("d2")["hand_alerted"])

    def test_unifi_error_reading_at_end_one_row_per_outage(self):
        eng = self.held()
        self.mock.fail_paths = [("GET", r"/api/v1/developer/doors/d2/lock_rule")]
        for m in (1, 2, 3):
            eng.at(2027, 9, 6, 20, m)
            with self.assertLogs("doorcontrol", "WARNING"):
                eng.tick()
        rows = read_rows(eng, "read door")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(eng.db.get_state("d2")["applied"], "unlock")
        self.mock.fail_paths = []
        eng.at(2027, 9, 6, 20, 4)
        eng.tick()
        self.assertEqual(eng.db.get_state("d2")["applied"], "free")
        self.assertEqual([r["ok"] for r in read_rows(eng, "read door")], [1, 0])


class Chains(EngineCase):
    """Review S11 (and R3): no calendar hold over 24 h without a break."""

    def allday_daily(self, **cal_cfg):
        ev = vevent(
            "Unlock: Office",
            uid="daily",
            start="VALUE=DATE:20270906",
            end="VALUE=DATE:20270907",
            extra=["RRULE:FREQ=DAILY"],
        )
        return self.make(vcal(ev), cal_cfg=cal_cfg)

    def test_daily_all_day_rrule_does_not_hold_for_days(self):
        eng = self.allday_daily()
        eng.at(2027, 9, 6, 12)
        eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "keep_unlock")
        n = len(self.mock.calls)
        eng.at(2027, 9, 7, 0, 1)
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2", n), ["reset"])
        for day in (7, 8, 9, 12):
            eng.at(2027, 9, day, 12)
            eng.tick()
            self.assertEqual(self.mock.rules["d2"]["type"], "schedule", day)
        texts = [t for _, t, _ in self.rec.of("calendar")]
        self.assertTrue(any("more than 24 hours in a row" in t for t in texts))

    def test_all_day_with_buffers_accepted(self):
        eng = self.make(
            vcal(
                vevent(
                    "Unlock: Office",
                    uid="ad",
                    start="VALUE=DATE:20270906",
                    end="VALUE=DATE:20270907",
                )
            ),
            cal_cfg={"buffer_before_min": 30, "buffer_after_min": 30},
        )
        eng.at(2027, 9, 6, 12)
        eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "keep_unlock")

    def test_back_to_back_timed_events_capped(self):
        ev = lambda uid, s, e: vevent(
            "Unlock: Office",
            uid=uid,
            start="TZID=America/New_York:" + s,
            end="TZID=America/New_York:" + e,
        )
        eng = self.make(
            vcal(
                ev("a", "20270906T200000", "20270907T080000"),
                ev("b", "20270907T080000", "20270907T200000"),
                ev("c", "20270907T200000", "20270908T060000"),
                ev("other", "20270907T200000", "20270907T230000"),
            )
        )
        eng.at(2027, 9, 7, 12)
        eng.tick()
        self.assertEqual(
            self.mock.rules["d2"]["type"], "keep_unlock", "24 h is allowed"
        )
        n = len(self.mock.calls)
        eng.at(2027, 9, 7, 20, 1)
        eng.tick()
        self.assertEqual(put_types(self.mock, "d2", n), ["reset"], "a break after 24 h")
        self.assertEqual(self.mock.rules["d1"]["type"], "schedule")
        texts = [t for _, t, _ in self.rec.of("calendar")]
        self.assertTrue(any("more than 24 hours in a row" in t for t in texts))

    def test_max_window_hours_clamped(self):
        self.assertEqual(calendar(max_window_hours=72).max_window_hours, 24)
        self.assertEqual(calendar(max_window_hours=0).max_window_hours, 1)
        self.assertEqual(calendar(max_window_hours=12).max_window_hours, 12)


class SmallFixes(unittest.TestCase):
    def test_titles_in_refusal_alerts_have_no_links(self):
        rec = Recorder()
        c = calendar(
            vcal(
                vevent(
                    "Unlock: Office see https://evil.example/x?y=1 or www.bad.example",
                    organizer="stranger@example.com",
                )
            ),
            rec,
        )
        with self.assertLogs("doorcontrol", "WARNING"):
            windows(c)
        (alert,) = rec.of("calendar")
        self.assertNotIn("evil.example", alert[1])
        self.assertNotIn("bad.example", alert[1])
        self.assertIn("[link]", alert[1])

    def test_feed_with_byte_order_mark(self):
        c = calendar("﻿" + vcal(vevent("Unlock: Office")))
        self.assertEqual(len(windows(c)), 1)

    def test_dev_http_alert_at_startup(self):
        rec = Recorder()
        self.assertFalse(app.warn_dev_mode("pinned", rec))
        self.assertEqual(rec.calls, [])
        self.assertTrue(app.warn_dev_mode("development", rec))
        (call,) = rec.calls
        self.assertIn("DOORCONTROL_DEV_ALLOW_HTTP", call[1])

    def test_docs_for_r2_r4_q4_s12(self):
        readme = read("README.md")
        for text in (
            '"allowed_organizers": []',
            "13 years",
            "40 Telegram messages a minute",
            "who can add or edit events on that calendar",
        ):
            self.assertIn(text, readme)


class DeployBlocks(unittest.TestCase):
    def test_deploy_runs_when_app_down_and_restore_waits(self):
        block = section_block(read("CLAUDE.md"), "### Deploy\n")
        self.assertNotIn("Stop: Door Control is not running", block)
        self.assertIn("deploying anyway", block)
        restore = block.split("restore_prev() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("Health.Status", restore)

    def prune_line(self, heading):
        block = section_block(read("CLAUDE.md"), heading)
        (line,) = [l for l in block.splitlines() if l.startswith("for P in deploy")]
        return line

    def test_prune_keeps_newest_10(self):
        for heading in ("### Deploy\n", "### Rollback to the previous Docker version"):
            d = tempfile.mkdtemp()
            for i in range(14):
                os.mkdir(os.path.join(d, "deploy-20270101-%06d" % i))
                os.mkdir(os.path.join(d, "pre-rollback-20270101-%06d" % i))
            os.mkdir(os.path.join(d, "other"))
            subprocess.run(
                ["bash", "-c", 'BKROOT="$1"; ' + self.prune_line(heading), "x", d],
                check=True,
            )
            left = sorted(os.listdir(d))
            self.assertEqual(len([x for x in left if x.startswith("deploy-")]), 10)
            self.assertEqual(len([x for x in left if x.startswith("pre-rollback")]), 10)
            self.assertIn("deploy-20270101-000013", left)
            self.assertNotIn("deploy-20270101-000003", left)
            self.assertIn("other", left)

    def test_rollback_name_checked(self):
        block = section_block(
            read("CLAUDE.md"), "### Rollback to the previous Docker version"
        )
        (line,) = [l for l in block.splitlines() if l.startswith('[[ "$NAME"')]
        self.assertLess(block.index(line), block.index("BK=$BKROOT/$NAME"))
        for name, ok in (
            ("deploy-20261201-093000", True),
            ("../deploy-20261201-093000", False),
            ("deploy-1/../../etc", False),
            ("deploy-", False),
            ("", False),
        ):
            r = subprocess.run(
                ["bash", "-c", 'NAME="$1"; ' + line + "; echo ok", "x", name],
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.stdout.strip() == "ok", ok, name)


if __name__ == "__main__":
    unittest.main()
