"""Shared helpers for test_safety.py and test_alerts.py: fake clock engine, recording
notifier (fake Telegram), calendar text builder. Nothing here contacts a real service.
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mock_unifi  # noqa: E402
from doorcontrol.db import DB  # noqa: E402
from doorcontrol.engine import Engine, validate_schedule  # noqa: E402
from doorcontrol.notify import Notifier  # noqa: E402
from doorcontrol.unifi import UnifiClient  # noqa: E402

TZ = ZoneInfo("America/New_York")
UTC = timezone.utc
JAKE = "jake@hardincountyfair.org"
KOLT = "kolt@hardincountyfair.org"
BOT_TOKEN = "123456789:AAFakeTelegramTokenForTests_xyz"


class FakeClock:
    def __init__(self, t=2_000_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Recorder(Notifier):
    """A real Notifier whose 'Telegram' is a list. Records every alert() call too."""

    def __init__(self, cfg=None, clock=time.time, send=None, chats=(1,), **kw):
        self.messages = []  # (chat, text) actually sent
        self.calls = []  # (kind, text, key) as asked
        super().__init__(
            BOT_TOKEN,
            list(chats),
            cfg,
            send=send or (lambda chat, text: self.messages.append((chat, text))),
            clock=clock,
            **kw,
        )

    def alert(self, kind, text, key=None):
        self.calls.append((kind, text, key))
        return super().alert(kind, text, key)

    def drain(self):
        for _ in range(1000):
            if self.process() != 0:
                return

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]

    def texts(self):
        self.drain()
        return [t for _, t in self.messages]


class Clock(Engine):
    """Engine with a controllable clock (local time)."""

    fake_now = None

    def now(self):
        return self.fake_now or super().now()

    def at(self, y, mo, d, h, mi=0):
        self.fake_now = datetime(y, mo, d, h, mi, tzinfo=TZ)

    def at_utc(self, dt):
        self.fake_now = dt.astimezone(TZ)


def build(calendar=None, notifier=None, cfg=None, timeout=10, db_path=None):
    mock = mock_unifi.Mock()
    srv = mock_unifi.make_server(mock)
    client = UnifiClient(
        "http://127.0.0.1:%d" % srv.server_address[1],
        mock_unifi.TOKEN,
        timeout=timeout,
    )
    db = DB(db_path or os.path.join(tempfile.mkdtemp(), "t.db"))
    eng = Clock(
        {"timezone": "America/New_York", **(cfg or {})},
        db,
        client,
        calendar,
        notifier or Recorder(),
    )
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


def vevent(
    summary,
    start="TZID=America/New_York:20270906T180000",
    end="TZID=America/New_York:20270906T210000",
    uid="u1",
    status="CONFIRMED",
    organizer=JAKE,
    extra=(),
):
    lines = ["BEGIN:VEVENT", "UID:" + uid]
    if status:
        lines.append("STATUS:" + status)
    if organizer:
        lines.append("ORGANIZER;CN=Someone:mailto:" + organizer)
    lines.append("DTSTART;" + start)
    if end:
        lines.append("DTEND;" + end)
    lines.append("SUMMARY:" + summary)
    lines += list(extra)
    lines.append("END:VEVENT")
    return "\n".join(lines)


def vcal(*events):
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + "\n".join(events) + "\nEND:VCALENDAR\n"


def put_types(mock, door=None, since=0):
    out = []
    for m, path, body in mock.calls[since:]:
        if m == "PUT" and (door is None or "/%s/" % door in path):
            out.append(body.get("type") or "unlock")
    return out
