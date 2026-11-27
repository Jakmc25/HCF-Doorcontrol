import base64
import hashlib
import json
import os
import sys
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mock_unifi
from doorcontrol import ical, unifi
from doorcontrol.db import DB
from doorcontrol.engine import (
    Engine,
    DoorError,
    schedule_occurrences,
    validate_schedule,
)
from doorcontrol.telegram_bot import Bot, split_duration
from doorcontrol.unifi import UnifiClient, UnifiError
from doorcontrol.web import make_server

TZ = ZoneInfo("America/New_York")


class Clock(Engine):
    """Engine with a controllable clock."""

    fake_now = None

    def now(self):
        return self.fake_now


def build(calendar=None):
    mock = mock_unifi.Mock()
    srv = mock_unifi.make_server(mock)
    client = UnifiClient(
        "http://127.0.0.1:%d" % srv.server_address[1], mock_unifi.TOKEN
    )
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    eng = Clock({"timezone": "America/New_York"}, db, client, calendar)
    return mock, srv, eng


def sched(**kw):
    base = {
        "name": "Office hours",
        "doors": ["d2"],
        "days": [0, 1, 2, 3, 4],
        "date": None,
        "start": "08:00",
        "end": "17:00",
        "end_action": "reset",
        "enabled": True,
    }
    base.update(kw)
    return validate_schedule(base)


class ScheduleMath(unittest.TestCase):
    def test_overnight_and_midnight(self):
        s = sched(start="18:00", end="00:00", days=[4])
        ((st, en),) = schedule_occurrences(
            s, date(2027, 9, 10), date(2027, 9, 10), TZ
        )  # Fri
        self.assertEqual((st.hour, en.day, en.hour), (18, 11, 0))

    def test_one_off_date(self):
        s = sched(days=None, date="2027-09-11")
        self.assertEqual(
            len(schedule_occurrences(s, date(2027, 9, 10), date(2027, 9, 12), TZ)), 1
        )
        self.assertEqual(
            len(schedule_occurrences(s, date(2027, 9, 12), date(2027, 9, 20), TZ)), 0
        )

    def test_validation(self):
        for bad in (
            {"name": ""},
            {"doors": []},
            {"start": "8am"},
            {"start": "08:00", "end": "08:00"},
            {"days": None, "date": None},
            {"days": [1], "date": "2027-01-01"},
            {"end_action": "boom"},
        ):
            with self.assertRaises(ValueError):
                sched(**bad)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.mock, self.srv, self.eng = build()

    def tearDown(self):
        self.srv.shutdown()

    def at(self, y, mo, d, h, mi=0):
        self.eng.fake_now = datetime(y, mo, d, h, mi, tzinfo=TZ)

    def test_unlock_then_relock_and_catchup(self):
        self.eng.db.add_schedule(sched())  # Mon-Fri 08-17 on d2
        self.at(2027, 9, 6, 7, 59)  # Monday
        self.eng.tick()
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "lock")
        self.at(2027, 9, 6, 8, 0)
        self.eng.tick()
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "unlock")
        n = len(self.mock.calls)
        self.at(2027, 9, 6, 12, 0)
        self.eng.tick()
        puts = [c for c in self.mock.calls[n:] if c[0] == "PUT"]
        self.assertEqual(puts, [], "must not re-send while window stays active")
        self.at(2027, 9, 6, 17, 0)
        self.eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "schedule")  # reset
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "lock")
        self.assertEqual(
            self.mock.rules["d1"]["type"], "schedule", "other doors untouched"
        )

    def test_restart_mid_window_applies(self):
        self.eng.db.add_schedule(sched())
        self.at(2027, 9, 7, 13, 0)
        self.eng.tick()
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "unlock")

    def test_keep_lock_end_action(self):
        self.eng.db.add_schedule(sched(end_action="keep_lock"))
        self.at(2027, 9, 6, 9)
        self.eng.tick()
        self.at(2027, 9, 6, 17, 1)
        self.eng.tick()
        self.assertEqual(self.mock.rules["d2"]["type"], "keep_lock")

    def test_manual_change_is_respected_at_end(self):
        self.eng.db.add_schedule(sched())
        self.at(2027, 9, 6, 9)
        self.eng.tick()
        self.eng.door_action(
            self.eng.find_door("office"), "lock"
        )  # someone locks it by hand
        n = len(self.mock.calls)
        self.at(2027, 9, 6, 17, 5)
        self.eng.tick()
        self.assertEqual([c for c in self.mock.calls[n:] if c[0] == "PUT"], [])
        self.assertEqual(self.eng.db.get_state("d2")["applied"], "free")

    def test_failure_retries(self):
        self.eng.db.add_schedule(sched())
        self.at(2027, 9, 6, 9)
        self.mock.fail = True
        with self.assertRaises(UnifiError):
            self.eng.tick()
        self.mock.fail = False
        self.eng.tick()
        self.assertEqual(self.mock.doors["d2"]["door_lock_relay_status"], "unlock")

    def test_paused_and_all_doors(self):
        sid = self.eng.db.add_schedule(
            sched(doors=["*"], name="Fair week", enabled=False)
        )
        self.at(2027, 9, 6, 9)
        self.eng.tick()
        self.assertTrue(
            all(d["door_lock_relay_status"] == "lock" for d in self.mock.doors.values())
        )
        self.eng.db.update_schedule(sid, sched(doors=["*"], name="Fair week"))
        self.eng.tick()
        self.assertTrue(
            all(
                d["door_lock_relay_status"] == "unlock"
                for d in self.mock.doors.values()
            )
        )

    def test_find_door(self):
        self.assertEqual(self.eng.find_door("event")["id"], "d1")
        with self.assertRaises(DoorError):
            self.eng.find_door("garage")

    def test_manual_actions(self):
        d = self.eng.find_door("main gate")
        self.eng.door_action(d, "unlock_for", 9999)
        self.assertEqual(self.mock.calls[-1][2], {"type": "custom", "interval": 480})
        self.eng.door_action(d, "pulse")
        self.assertTrue(self.mock.calls[-1][1].endswith("/unlock"))


ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:a1
DTSTART;TZID=America/New_York:20270906T180000
DTEND;TZID=America/New_York:20270906T210000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4
EXDATE;TZID=America/New_York:20270908T180000
SUMMARY:Unlock: Event Center\\, Main Gate
END:VEVENT
BEGIN:VEVENT
UID:a1
RECURRENCE-ID;TZID=America/New_York:20270913T180000
DTSTART;TZID=America/New_York:20270913T190000
DTEND;TZID=America/New_York:20270913T200000
SUMMARY:Unlock: Event Center
END:VEVENT
BEGIN:VEVENT
UID:b2
DTSTART:20270910T120000Z
DTEND:20270910T140000Z
SUMMARY:Dentist
END:VEVENT
BEGIN:VEVENT
UID:c3
DTSTART;VALUE=DATE:20270911
DTEND;VALUE=DATE:20270912
SUMMARY:unlock: ALL
END:VEVENT
END:VCALENDAR
"""


class CalendarTests(unittest.TestCase):
    def test_parse_and_expand(self):
        cal = ical.Calendar({"ical_url": "x"}, TZ)
        cal.load_text(ICS)
        first = datetime(2027, 9, 1, tzinfo=TZ)
        wins = cal.windows(first, first + timedelta(days=30))
        got = sorted(
            (w["start"].strftime("%m-%d %H:%M"), w["end"].strftime("%H:%M"))
            for w in wins
        )
        # COUNT=4 -> 9/6, 9/8(excluded), 9/13(overridden to 19:00), 9/15 ; plus the override and the all-day
        self.assertEqual(
            got,
            [
                ("09-06 18:00", "21:00"),
                ("09-11 00:00", "00:00"),
                ("09-13 19:00", "20:00"),
                ("09-15 18:00", "21:00"),
            ],
        )
        self.assertEqual(
            wins[0]["door_queries"]
            in (["Event Center", "Main Gate"], ["*"], ["Event Center"]),
            True,
        )

    def test_calendar_drives_unlock(self):
        cal = ical.Calendar({"ical_url": "x", "buffer_before_min": 15}, TZ)
        cal.load_text(ICS)
        mock, srv, eng = build(cal)
        try:
            eng.fake_now = datetime(2027, 9, 6, 17, 50, tzinfo=TZ)
            eng.tick()
            self.assertEqual(mock.doors["d1"]["door_lock_relay_status"], "unlock")
            self.assertEqual(mock.doors["d3"]["door_lock_relay_status"], "unlock")
            self.assertEqual(mock.doors["d2"]["door_lock_relay_status"], "lock")
            eng.fake_now = datetime(2027, 9, 6, 21, 0, tzinfo=TZ)
            eng.tick()
            self.assertEqual(mock.doors["d1"]["door_lock_relay_status"], "lock")
        finally:
            srv.shutdown()


class TelegramTests(unittest.TestCase):
    def test_commands(self):
        mock, srv, eng = build()
        try:
            bot = Bot({"allowed_chat_ids": [42]}, eng)
            self.assertIn("Not authorized", bot.handle(7, "/doors"))
            self.assertIn("7", bot.handle(7, "/start"))
            self.assertIn("Event Center", bot.handle(42, "/doors"))
            self.assertIn(
                "unlocked for 120 min", bot.handle(42, "/unlock event center 2h")
            )
            self.assertEqual(mock.calls[-1][2], {"type": "custom", "interval": 120})
            self.assertIn("locked now", bot.handle(42, "/lock event center"))
            self.assertIn("No door matches", bot.handle(42, "/unlock garage"))
            self.assertIn("Which door", bot.handle(42, "/unlock"))
            self.assertEqual(split_duration("main gate 1h30m"), ("main gate", 90))
            self.assertEqual(split_duration("gate 2"), ("gate 2", None))
            mock.fail = True
            self.assertIn("UniFi problem", bot.handle(42, "/lock office"))
        finally:
            srv.shutdown()


class WebTests(unittest.TestCase):
    HOST = "app.test"

    def setUp(self):
        self.mock, self.usrv, self.eng = build()
        self.eng.fake_now = datetime(2027, 9, 6, 9, 0, tzinfo=TZ)
        env = {
            "PUBLIC_HOSTS": self.HOST,
            "ALLOWED_HOSTS": "",
            "GOOGLE_ALLOWED_DOMAINS": "hardincountyfair.org",
            "GOOGLE_ALLOWED_EMAILS": "",
            "TRUSTED_PROXIES": "127.0.0.1/32",  # the test client plays cloudflared
        }
        with mock.patch.dict(os.environ, env):
            self.web = make_server(self.eng, tempfile.mkdtemp(), "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.web.server_address[1]
        # A test session: a Google cookie issued by this server, plus the CSRF token.
        self.cookie = "doors_auth=" + self.web.google.issue(
            "tester@hardincountyfair.org"
        )
        self.csrf = None
        code, raw = self.call("GET", "/api/bootstrap")
        self.assertEqual(code, 200, raw)
        self.csrf = json.loads(raw)["csrf"]

    def tearDown(self):
        self.web.shutdown()
        self.usrv.shutdown()

    def call(self, method, path, body=None, headers=None, session=True):
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Host", self.HOST)
        if session:
            req.add_header("Cookie", self.cookie)
            if self.csrf:
                req.add_header("X-CSRF-Token", self.csrf)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                if "doors_session=" in (r.headers.get("Set-Cookie") or ""):
                    self.cookie += "; " + r.headers["Set-Cookie"].split(";")[0]
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_basic_header_refused(self):
        basic = "Basic " + base64.b64encode(b"u:pw").decode()
        code, raw = self.call(
            "GET", "/api/doors", headers={"Authorization": basic}, session=False
        )
        self.assertEqual(code, 401)
        self.assertNotIn(b"Event Center", raw)
        self.assertEqual(self.call("GET", "/api/doors")[0], 200)
        self.assertIn(b"Door Control", self.call("GET", "/")[1])

    def test_schedule_crud_and_actions(self):
        body = {
            "name": "Office",
            "doors": ["d2"],
            "days": [0],
            "start": "08:00",
            "end": "17:00",
            "end_action": "reset",
        }
        code, raw = self.call("POST", "/api/schedules", body)
        self.assertEqual(code, 201)
        sid = json.loads(raw)["id"]
        self.assertEqual(
            self.call("POST", "/api/schedules", {**body, "start": "nope"})[0], 400
        )
        self.assertEqual(len(json.loads(self.call("GET", "/api/schedules")[1])), 1)
        up = json.loads(self.call("GET", "/api/upcoming")[1])
        self.assertTrue(up and up[0]["doors"] == ["Office"])
        self.assertEqual(
            self.call("PUT", "/api/schedules/%d" % sid, {**body, "name": "Renamed"})[0],
            200,
        )
        self.assertEqual(self.call("PUT", "/api/schedules/999", body)[0], 404)
        c, raw = self.call(
            "POST", "/api/doors/event/action", {"action": "unlock_for", "minutes": 30}
        )
        self.assertEqual(c, 200, raw)
        self.assertEqual(
            self.call("POST", "/api/doors/nope/action", {"action": "lock"})[0], 404
        )
        self.assertEqual(
            self.call("POST", "/api/doors/event/action", {"action": "explode"})[0], 400
        )
        self.assertEqual(self.call("DELETE", "/api/schedules/%d" % sid)[0], 200)
        logs = json.loads(self.call("GET", "/api/log")[1])
        self.assertTrue(any("unlocked for 30" in r["detail"] for r in logs))
        statuses = json.loads(self.call("GET", "/api/doors")[1])
        self.assertEqual(
            {d["name"] for d in statuses}, {"Event Center", "Office", "Main Gate"}
        )


class UnifiTlsTests(unittest.TestCase):
    """S3: optional pinning of the console certificate (checked before the token is sent)."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.cert = os.path.join(cls.dir, "c.pem")
        cls.key = os.path.join(cls.dir, "k.pem")
        try:
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "2",
                    "-subj",
                    "/CN=127.0.0.1",
                    "-keyout",
                    cls.key,
                    "-out",
                    cls.cert,
                ],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            raise unittest.SkipTest("openssl not available")
        der = ssl.PEM_cert_to_DER_cert(open(cls.cert).read())
        cls.fp = hashlib.sha256(der).hexdigest()

    def setUp(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.mock = mock_unifi.Mock()
        self.srv = mock_unifi.make_server(self.mock, ssl_context=ctx)
        self.url = "https://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_matching_fingerprint_connects(self):
        colons = ":".join(self.fp[i : i + 2] for i in range(0, 64, 2)).upper()
        c = UnifiClient(
            self.url, mock_unifi.TOKEN, cert_sha256="sha256 Fingerprint=" + colons
        )
        self.assertTrue(c.pinned)
        self.assertEqual(len(c.doors()), 3)

    def test_wrong_fingerprint_refused_before_token(self):
        c = UnifiClient(self.url, mock_unifi.TOKEN, cert_sha256="0" * 64)
        with self.assertRaises(UnifiError) as e:
            c.doors()
        self.assertIn("certificate", str(e.exception))
        self.assertEqual(self.mock.calls, [], "the request (and token) was never sent")

    def test_unpinned_keeps_todays_behavior(self):
        c = UnifiClient(self.url, mock_unifi.TOKEN)
        self.assertFalse(c.pinned)
        self.assertEqual(len(c.doors()), 3)

    def test_fingerprint_format(self):
        self.assertEqual(unifi.normalize_fingerprint("AB:" * 31 + "AB"), "ab" * 32)
        for bad in ("", "abc", "zz" * 32):
            with self.assertRaises(ValueError):
                unifi.normalize_fingerprint(bad)
        self.assertTrue(unifi.cert_matches(b"x", hashlib.sha256(b"x").hexdigest()))
        self.assertFalse(unifi.cert_matches(b"y", hashlib.sha256(b"x").hexdigest()))


class PlainErrorTests(unittest.TestCase):
    """S8/S9: people see plain sentences; details stay in the server log."""

    def test_unifi_errors_are_plain(self):
        mock, srv, eng = build()
        try:
            mock.fail = True
            with self.assertLogs("doorcontrol", "WARNING") as logs:
                with self.assertRaises(UnifiError) as e:
                    eng.client.doors()
            self.assertEqual(
                str(e.exception),
                "The UniFi door controller refused the request (HTTP 500).",
            )
            self.assertIn("CODE_SYSTEM_ERROR", "\n".join(logs.output))
        finally:
            srv.shutdown()
        c = UnifiClient("http://127.0.0.1:1", "t", timeout=1)
        with self.assertLogs("doorcontrol", "WARNING"):
            with self.assertRaises(UnifiError) as e:
                c.doors()
        self.assertEqual(str(e.exception), "Cannot reach the UniFi door controller.")

    def test_calendar_error_is_plain(self):
        cal = ical.Calendar({"ical_url": "https://secret.example/abc.ics"}, TZ)

        def boom():
            raise OSError("failed https://secret.example/abc.ics")

        class Once(threading.Event):
            def wait(self, t=None):
                self.set()

        cal.fetch_once = boom
        cal._stop = Once()
        with self.assertLogs("doorcontrol", "WARNING"):
            cal._loop()
        self.assertNotIn("secret", cal.last_error)
        self.assertTrue(cal.last_error.endswith("."))

    def test_calendar_url_must_be_https_and_feed_capped(self):
        with self.assertRaises(ValueError) as e:
            ical.check_url("http://calendar.google.com/x.ics")
        self.assertIn("https://", str(e.exception))
        ical.check_url("https://calendar.google.com/x.ics")
        ical.check_url("")

        class Big(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b"x" * (ical.MAX_FEED_BYTES + 1)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # the reader stops at the cap

        srv = HTTPServer(("127.0.0.1", 0), Big)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cal = ical.Calendar(
                {"ical_url": "http://127.0.0.1:%d/" % srv.server_address[1]}, TZ
            )
            with self.assertRaises(ValueError):
                cal.fetch_once()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
