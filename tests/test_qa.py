"""QA probes for 1.1.0 (PR 1): extra RRULE abuse, startup-refusal edge cases, Telegram
down for an hour with a hung sender, alert flood bounds. Fakes and mock_unifi.py only.
Run: python3 tests/test_qa.py
"""

import logging
import time
import unittest

from support import (
    JAKE,
    TZ,
    FakeClock,
    Recorder,
    build,
    vcal,
    vevent,
)

from datetime import datetime

from doorcontrol.app import check_unifi
from doorcontrol.ical import Calendar

PIN = "a" * 64
DEV = {"DOORCONTROL_DEV_ALLOW_HTTP": "1"}


class StartupRefusal(unittest.TestCase):
    def refused(self, u, env=None):
        with self.assertRaises(SystemExit):
            check_unifi(u, env or {})

    def test_http_and_unpinned_refused(self):
        self.refused({"host": "http://192.168.1.1:12445", "cert_sha256": PIN})
        self.refused({"host": "https://192.168.1.1:12445"})
        self.refused({"host": "https://192.168.1.1:12445", "verify_tls": "true"})
        for host in ("", None):
            self.refused({"host": host, "cert_sha256": PIN})

    def test_dev_hatch_is_exactly_one_and_loopback_http_only(self):
        self.assertEqual(
            check_unifi({"host": "http://127.0.0.1:12445"}, DEV), "development"
        )
        for val in ("true", "yes", "0", "", " 1"):
            self.refused(
                {"host": "http://127.0.0.1:12445"}, {"DOORCONTROL_DEV_ALLOW_HTTP": val}
            )
        for host in (
            "http://192.168.1.1:12445",
            "http://127.0.0.1@evil.example:12445",
            "http://localhost.evil.example",
            "http://[::1]:1",
        ):
            self.refused({"host": host}, DEV)


class RruleAbuse(unittest.TestCase):
    RULES = [
        "FREQ=MONTHLY;BYMONTHDAY=-40",
        "FREQ=YEARLY;BYMONTH=13",
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30",
        "FREQ=DAILY;COUNT=99999999999",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=SECONDLY",
        "FREQ=DAILY;UNTIL=garbage",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=MONTHLY;BYDAY=5MO",
        "FREQ=WEEKLY;INTERVAL=999999",
        "FREQ=DAILY;COUNT=-1",
        "FREQ=;",
        "BYDAY=MO",
        "FREQ=MONTHLY;BYMONTHDAY=31;BYDAY=MO",
    ]

    def test_none_hangs_or_raises(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        first = datetime(2027, 9, 1, tzinfo=TZ)
        last = datetime(2027, 9, 30, tzinfo=TZ)
        for start in ("TZID=America/New_York:20270101T180000", "VALUE=DATE:20270101"):
            for rule in self.RULES:
                with self.subTest(start=start, rule=rule):
                    end = (
                        "TZID=America/New_York:20270101T210000"
                        if "TZID" in start
                        else "VALUE=DATE:20270102"
                    )
                    ev = vevent(
                        "Unlock: Office",
                        start=start,
                        end=end,
                        extra=["RRULE:" + rule],
                    )
                    c = Calendar(
                        {
                            "ical_url": "https://cal.example/x.ics",
                            "allowed_organizers": [JAKE],
                        },
                        TZ,
                        notifier=Recorder(),
                    )
                    c.load_text(vcal(ev))
                    t0 = time.monotonic()
                    out = c.windows(first, last)
                    self.assertLess(time.monotonic() - t0, 2)
                    self.assertLess(len(out), 40)


class TelegramDownAnHour(unittest.TestCase):
    def test_hung_sender_never_slows_ticks_and_queue_is_bounded(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        clk = FakeClock()
        stuck = __import__("threading").Event()
        self.addCleanup(stuck.set)
        n = Recorder(clock=clk, send=lambda chat, text: stuck.wait(30))
        n.start()
        self.addCleanup(n.stop)
        _, _, eng = build(notifier=n)
        doors = eng.doors()
        t0 = time.monotonic()
        for i in range(120):  # one hour of 30 s ticks with doors changing
            clk.advance(30)
            eng.door_action(
                doors[i % len(doors)],
                "keep_unlock" if i % 2 == 0 else "reset",
                source="telegram",
                user="telegram:1/x",
            )
            eng.tick()
        self.assertLess(time.monotonic() - t0, 5)
        for i in range(2000):
            n.alert("status", "x%d" % i, key=("k", i))
        s = n.stats()
        self.assertLessEqual(s["queued"], n.queue_size)
        self.assertGreater(s["dropped_low"], 0)


if __name__ == "__main__":
    unittest.main()
