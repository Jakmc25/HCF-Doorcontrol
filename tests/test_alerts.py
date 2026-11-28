"""1.1.0 Telegram alerts (notifier queue, dedupe, cap, scrub, never blocking) and Telegram
access (replay, offset, rate limit, groups, attribution).

Fake Telegram (support.Recorder and a scripted getUpdates), fake clocks, mock_unifi.py.
Run: python3 tests/test_alerts.py
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from unittest import mock

from support import (
    BOT_TOKEN,
    JAKE,
    TZ,
    FakeClock,
    Recorder,
    build,
    put_types,
    sched,
    vcal,
    vevent,
)

from doorcontrol import web
from doorcontrol.db import DB
from doorcontrol.ical import Calendar
from doorcontrol.notify import Notifier
from doorcontrol.telegram_bot import Bot
from doorcontrol.unifi import UnifiError

UNIFI_TOKEN = "unifi-secret-token-abcdef"
CAL_URL = "https://calendar.google.com/calendar/ical/secret123/basic.ics"


# ---------------------------------------------------------------- C. notifier
class NotifierTests(unittest.TestCase):
    def test_no_token_or_no_chats_logs_only_never_raises(self):
        for n in (Notifier("", [5]), Notifier(BOT_TOKEN, []), Notifier()):
            with self.subTest(n=n):
                self.assertFalse(n.enabled)
                with self.assertLogs("doorcontrol", "INFO"):
                    self.assertFalse(n.alert("status", "Office: locked now. By x."))
                n.start()
                self.assertIsNone(n._thread)
                self.assertIsNone(n.process())
        # Anything odd going in never raises.
        n = Recorder()
        self.assertTrue(n.alert("status", object()))
        with mock.patch.object(n, "_alert", side_effect=RuntimeError("x")):
            with self.assertLogs("doorcontrol", "ERROR"):
                self.assertFalse(n.alert("status", "x"))

    def test_error_alerts_plain_text_no_token_no_url(self):
        n = Recorder(secrets=[UNIFI_TOKEN, CAL_URL])
        n.alert("door_error", "failed with token %s at %s" % (UNIFI_TOKEN, CAL_URL))
        n.alert("calendar", "bot%s leaked" % BOT_TOKEN)
        n.alert("calendar", "see https://calendar.google.com/calendar/ical/other/x.ics")
        for text in n.texts():
            for secret in (UNIFI_TOKEN, BOT_TOKEN, "secret123", "calendar.google.com"):
                self.assertNotIn(secret, text)
            self.assertIn("[hidden]", text)

    def test_calendar_feed_failure_alert_hides_url(self):
        rec = Recorder(secrets=[CAL_URL])
        c = Calendar({"ical_url": CAL_URL}, TZ, notifier=rec)

        def boom():
            raise urllib.error.URLError("failed to reach " + CAL_URL)

        c.fetch_once = boom
        with self.assertLogs("doorcontrol", "WARNING"):
            c.refresh_once()
        (text,) = rec.texts()
        self.assertIn("could not be read", text)
        self.assertNotIn("secret123", text)

    def test_dedupe_then_repeat_summary_after_30_min(self):
        clock = FakeClock()
        n = Recorder(clock=clock)
        for _ in range(5):
            n.alert("door_error", "Office failed", key=("door", "d2", "lock"))
            clock.advance(120)
        self.assertEqual(n.texts(), ["Office failed"])
        clock.advance(25 * 60)
        n.alert("door_error", "Office failed", key=("door", "d2", "lock"))
        texts = n.texts()
        self.assertEqual(len(texts), 2)
        self.assertRegex(
            texts[1], r"^Office failed \(repeated 4 times since \d\d:\d\d\)$"
        )
        # Other keys are separate; status changes (no key) are never deduped.
        n.alert("door_error", "Gate failed", key=("door", "d3", "lock"))
        n.alert("status", "Office: locked now.")
        n.alert("status", "Office: locked now.")
        self.assertEqual(len(n.texts()), 5)

    def test_cap_20_per_minute_then_one_summary(self):
        clock = FakeClock()
        n = Recorder(clock=clock)
        for i in range(25):
            n.alert("door_error", "problem %d" % i)
        n.drain()
        self.assertEqual(len(n.messages), 20)
        self.assertGreater(n.process(), 0, "waits for the minute to open")
        clock.advance(61)
        n.drain()
        self.assertEqual(len(n.messages), 21)
        summary = n.messages[-1][1]
        self.assertTrue(summary.startswith("5 more alerts"), summary)
        for i in range(20, 25):
            self.assertIn("problem %d" % i, summary)

    def test_full_queue_drops_oldest_low_and_counts(self):
        n = Recorder(cfg={"queue_size": 3})
        n.alert("status", "low 1")
        n.alert("door_error", "high 1")
        n.alert("status", "low 2")
        n.alert("door_error", "high 2")
        self.assertEqual([i["text"] for i in n._q], ["high 1", "low 2", "high 2"])
        self.assertEqual((n.dropped_low, n.dropped_high), (1, 0))
        texts = n.texts()
        self.assertIn("1 older alerts were dropped", texts[0])

    def test_high_drop_logged(self):
        n = Recorder(cfg={"queue_size": 2})
        n.alert("door_error", "high 1")
        n.alert("door_error", "high 2")
        with self.assertLogs("doorcontrol", "WARNING") as logs:
            n.alert("door_error", "high 3")
        self.assertIn("dropped an alert: high 1", "\n".join(logs.output))
        self.assertEqual(n.dropped_high, 1)
        self.assertEqual(n.stats()["dropped_high"], 1)

    def test_telegram_errors_retry_with_backoff(self):
        clock = FakeClock()
        fail = [True]
        got = []

        def send(chat, text):
            if fail[0]:
                raise urllib.error.URLError("down")
            got.append((chat, text))

        n = Recorder(clock=clock, send=send, chats=(1, 2))
        n.alert("door_error", "Office failed")
        with self.assertLogs("doorcontrol", "WARNING"):
            self.assertEqual(n.process(), 0)
        self.assertEqual(n.process(), 5.0, "backoff")
        fail[0] = False
        clock.advance(5)
        n.drain()
        self.assertEqual(got, [(1, "Office failed"), (2, "Office failed")])

    def test_switched_off_kind_logged_only(self):
        n = Recorder(cfg={"kinds": {"status": False}})
        with self.assertLogs("doorcontrol", "INFO") as logs:
            self.assertFalse(n.alert("status", "Office: locked now."))
        self.assertIn("Office: locked now.", "\n".join(logs.output))
        self.assertTrue(n.alert("door_error", "x"))
        self.assertEqual(n.texts(), ["x"])

    def test_one_recovered_per_outage(self):
        n = Recorder()
        self.assertFalse(n.recovered("unifi", "back"), "no outage, no message")
        for name in ("unifi", "calendar_feed", "scheduler"):
            for _ in range(3):
                n.outage(name, "door_error", name + " down")
            n.recovered(name, name + " back")
            n.recovered(name, name + " back")
        texts = n.texts()
        for name in ("unifi", "calendar_feed", "scheduler"):
            self.assertEqual(texts.count(name + " down"), 1)
            self.assertEqual(texts.count(name + " back"), 1)
        n.outage("unifi", "door_error", "unifi down")  # a new outage alerts at once
        self.assertEqual(n.texts().count("unifi down"), 2)

    def test_alerts_skip_negative_chat_ids(self):
        with self.assertLogs("doorcontrol", "WARNING") as logs:
            n = Recorder(chats=(42, -1001234, -5))
        self.assertIn("group or channel", "\n".join(logs.output))
        n.alert("door_error", "x")
        n.drain()
        self.assertEqual([c for c, _ in n.messages], [42])
        self.assertFalse(Recorder(chats=(-5,)).enabled)

    def test_telegram_down_tick_duration_unchanged(self):
        def run(notifier):
            m, srv, eng = build(notifier=notifier)
            self.addCleanup(srv.shutdown)
            eng.db.add_schedule(sched(doors=["*"]))
            eng.at(2027, 9, 6, 7)
            eng.tick()  # warm up
            eng.at(2027, 9, 6, 9)
            t0 = time.monotonic()
            eng.tick()
            eng.door_action(eng.find_door("office"), "lock", source="web", user=JAKE)
            return time.monotonic() - t0

        base = run(Notifier())

        def slow(chat, text):
            time.sleep(3)
            raise urllib.error.URLError("Telegram is down")

        n = Recorder(send=slow)
        n.start()
        self.addCleanup(n.stop)
        with self.assertLogs("doorcontrol", "INFO"):
            took = run(n)
        self.assertLess(took, base + 0.5, (took, base))
        self.assertGreaterEqual(len(n.of("status")), 4)

    def test_real_sender_thread_delivers(self):
        got = threading.Event()
        n = Recorder(send=lambda chat, text: got.set())
        n.start()
        self.addCleanup(n.stop)
        n.alert("door_error", "x")
        self.assertTrue(got.wait(5))


# ---------------------------------------------------------------- C. hooks
class StatusAlerts(unittest.TestCase):
    def test_status_alert_from_web_telegram_schedule_calendar_with_source_and_end(
        self,
    ):
        rec = Recorder()
        cal = Calendar(
            {"ical_url": "https://x/y", "allowed_organizers": [JAKE]}, TZ, notifier=rec
        )
        cal.load_text(vcal(vevent("Unlock: Event Center")))
        m, srv, eng = build(cal, rec)
        self.addCleanup(srv.shutdown)
        eng.db.add_schedule(sched())
        eng.at(2027, 9, 6, 19)  # Monday 7 PM: the calendar event
        eng.tick()
        eng.at(2027, 9, 7, 9)  # Tuesday 9 AM: the schedule
        eng.tick()
        eng.door_action(
            eng.find_door("main gate"), "unlock_for", 30, source="web", user=JAKE
        )
        Bot({"allowed_chat_ids": [42]}, eng).handle(42, "/lock main gate", "kolt")
        texts = [t for _, t, _ in rec.of("status")]
        self.assertEqual(len(texts), 5, texts)
        self.assertEqual(
            texts.pop(1),
            "Fairgrounds - Event Center: back to its normal UniFi schedule. "
            "By scheduler: window ended.",
        )
        self.assertIn(
            "Fairgrounds - Event Center: kept unlocked until Mon 9:00 PM. "
            "By calendar:" + JAKE,
            texts[0],
        )
        self.assertIn(
            "Fairgrounds - Office: kept unlocked until Tue 5:00 PM. "
            "By scheduler: Office hours.",
            texts[1],
        )
        self.assertIn("unlocked for 30 min until Tue 9:30 AM. By " + JAKE, texts[2])
        self.assertIn("locked now. By telegram:42/kolt.", texts[3])

    def test_door_failure_from_web_alerts_at_once(self):
        rec = Recorder()
        m, srv, eng = build(notifier=rec)
        self.addCleanup(srv.shutdown)
        d = eng.find_door("office")
        m.fail = True
        with self.assertLogs("doorcontrol", "WARNING"):
            with self.assertRaises(UnifiError):
                eng.door_action(d, "lock", source="web", user=JAKE)
        (err,) = rec.of("door_error")
        self.assertIn("could not be told to lock", err[1])
        self.assertIn(JAKE, err[1])
        self.assertIn("Nothing was changed", err[1])


class ScheduleChangeAlerts(unittest.TestCase):
    """AC30 through the real web server with a signed-in user."""

    HOST = "app.test"

    def setUp(self):
        self.rec = Recorder()
        self.mock, self.usrv, self.eng = build(notifier=self.rec)
        self.eng.doors()
        env = {
            "PUBLIC_HOSTS": self.HOST,
            "ALLOWED_HOSTS": "",
            "GOOGLE_ALLOWED_DOMAINS": "hardincountyfair.org",
            "GOOGLE_ALLOWED_EMAILS": "",
            "TRUSTED_PROXIES": "127.0.0.1/32",
        }
        with mock.patch.dict(os.environ, env):
            self.web = web.make_server(self.eng, tempfile.mkdtemp(), "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.addCleanup(self.usrv.shutdown)
        self.addCleanup(self.web.server_close)
        self.addCleanup(self.web.shutdown)
        self.cookie = "doors_auth=" + self.web.google.issue(JAKE)
        self.csrf = None
        self.csrf = self.call("GET", "/api/bootstrap")[1]["csrf"]

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.web.server_address[1], path),
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Host", self.HOST)
        req.add_header("Cookie", self.cookie)
        if self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with urllib.request.urlopen(req) as r:
                sc = r.headers.get("Set-Cookie") or ""
                if "doors_session=" in sc:
                    self.cookie += "; " + sc.split(";")[0]
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_schedule_create_edit_delete_pause_resume_alert_with_email(self):
        body = {
            "name": "Office hours",
            "doors": ["d2"],
            "days": [0, 1, 2, 3, 4],
            "start": "08:00",
            "end": "17:00",
        }
        code, r = self.call("POST", "/api/schedules", body)
        self.assertEqual(code, 201)
        sid = r["id"]
        path = "/api/schedules/%d" % sid
        self.call("PUT", path, {**body, "end": "18:00"})
        self.call("PUT", path, {**body, "end": "18:00", "enabled": False})
        self.call("PUT", path, {**body, "end": "18:00", "enabled": True})
        self.call("DELETE", path)
        self.assertEqual(self.call("DELETE", path)[0], 404)
        texts = [t for _, t, _ in self.rec.of("schedule_change")]
        self.assertEqual(len(texts), 5, texts)
        self.assertEqual(
            texts[0],
            'Schedule "Office hours" added by %s: Mon, Tue, Wed, Thu, Fri '
            "08:00-17:00, Office." % JAKE,
        )
        self.assertIn("changed by " + JAKE, texts[1])
        self.assertIn("08:00-18:00", texts[1])
        self.assertEqual(texts[2], 'Schedule "Office hours" paused by %s.' % JAKE)
        self.assertEqual(texts[3], 'Schedule "Office hours" resumed by %s.' % JAKE)
        self.assertEqual(texts[4], 'Schedule "Office hours" deleted by %s.' % JAKE)
        rows = [r for r in self.eng.db.recent_log(20) if r["kind"] == "schedule"]
        self.assertEqual(len(rows), 5)
        h = self.call("GET", "/api/health")[1]
        self.assertEqual(h["calendar_state"], "off")
        self.assertIn("dropped_low", h["alerts"])
        self.assertTrue(h["scheduler_ok"])


# ---------------------------------------------------------------- D. Telegram access
def update(uid, text, chat=42, kind="private", age=0, now=None, name="jake"):
    now = time.time() if now is None else now
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "date": int(now - age),
            "chat": {"id": chat, "type": kind},
            "from": {"id": chat, "username": name},
            "text": text,
        },
    }


class FakeTelegram:
    """getUpdates returns the scripted updates at or after the offset (as Telegram)."""

    def __init__(self, updates):
        self.updates = list(updates)
        self.sent = []
        self.offsets = []

    def __call__(self, method, params):
        if method == "getUpdates":
            self.offsets.append(params["offset"])
            return {
                "ok": True,
                "result": [
                    u for u in self.updates if u["update_id"] >= params["offset"]
                ],
            }
        if method == "sendMessage":
            self.sent.append((params["chat_id"], params["text"]))
            return {"ok": True}
        raise AssertionError(method)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        self.mock, self.srv, self.eng = build(db_path=self.path)
        self.addCleanup(self.srv.shutdown)
        self.clock = FakeClock(time.time())

    def bot(self, api, **cfg):
        return Bot(
            {"token": "x", "allowed_chat_ids": [42], **cfg},
            self.eng,
            api=api,
            clock=self.clock,
        )

    def test_group_chat_and_callback_ignored_no_reply(self):
        tg = FakeTelegram(
            [
                update(1, "/unlock office", kind="group", chat=-100),
                update(2, "/unlock office", kind="supergroup", chat=-200),
                {"update_id": 3, "callback_query": {"id": "q", "data": "/unlock"}},
                {
                    "update_id": 4,
                    "edited_message": update(4, "/unlock office")["message"],
                },
                {
                    "update_id": 5,
                    "channel_post": {"chat": {"id": -3, "type": "channel"}},
                },
            ]
        )
        self.bot(tg).poll_once()
        self.assertEqual(tg.sent, [])
        self.assertEqual(put_types(self.mock), [])
        self.assertEqual(self.eng.db.kv_get("telegram_offset"), "6")

    def test_old_message_dropped_no_reply(self):
        tg = FakeTelegram([update(1, "/unlock office", age=121, now=self.clock())])
        with self.assertLogs("doorcontrol", "INFO") as logs:
            self.bot(tg).poll_once()
        self.assertEqual((tg.sent, put_types(self.mock)), ([], []))
        self.assertIn("dropped", "\n".join(logs.output))
        tg = FakeTelegram([update(2, "/lock office", age=30, now=self.clock())])
        self.bot(tg).poll_once()
        self.assertEqual(put_types(self.mock), ["lock_now"])

    def test_restart_reads_offset_old_unlock_not_run(self):
        tg = FakeTelegram([update(7, "/unlock office", now=self.clock())])
        self.bot(tg).poll_once()
        self.assertEqual(put_types(self.mock), ["custom"])
        # Restart: new Bot and engine objects on the same database; Telegram would
        # replay update 7 if asked from offset 0.
        m2, srv2, eng2 = build(db_path=self.path)
        self.addCleanup(srv2.shutdown)
        tg2 = FakeTelegram(tg.updates)
        Bot({"token": "x", "allowed_chat_ids": [42]}, eng2, api=tg2).poll_once()
        self.assertEqual(tg2.offsets, [8])
        self.assertEqual(put_types(m2), [])

    def test_offset_stored_before_handling(self):
        tg = FakeTelegram([update(9, "/unlock office", now=self.clock())])
        b = self.bot(tg)
        b.handle = mock.Mock(side_effect=RuntimeError("crash mid-command"))
        with self.assertRaises(RuntimeError):
            b.poll_once()
        self.assertEqual(self.eng.db.kv_get("telegram_offset"), "10")

    def test_rate_limit_one_slow_down_no_action(self):
        ups = [update(i, "/doors", now=self.clock()) for i in range(1, 11)]
        ups += [update(i, "/lock office", now=self.clock()) for i in range(11, 14)]
        tg = FakeTelegram(ups)
        self.bot(tg).poll_once()
        replies = [t for _, t in tg.sent]
        self.assertEqual(len(replies), 11)
        self.assertEqual(replies[-1], "Slow down: wait a minute.")
        self.assertEqual(put_types(self.mock), [], "no door action past the limit")
        self.clock.advance(61)
        tg = FakeTelegram([update(20, "/lock office", now=self.clock())])
        self.bot(tg).poll_once()

    def test_rate_limit_window_resets(self):
        b = self.bot(FakeTelegram([]), max_commands_per_minute=2)
        say = lambda i: b.process_update(update(i, "/doors", now=self.clock()))
        self.assertIsNotNone(say(1))
        self.assertIsNotNone(say(2))
        self.assertEqual(say(3), (42, "Slow down: wait a minute."))
        self.assertIsNone(say(4))
        self.clock.advance(61)
        self.assertIn("Event Center", say(5)[1])

    def test_door_action_logged_as_telegram_chat_name(self):
        b = self.bot(FakeTelegram([]))
        b.process_update(update(1, "/lock office", now=self.clock(), name="jake"))
        self.assertEqual(self.eng.db.recent_log(1)[0]["user"], "telegram:42/jake")
        b.handle(42, "/lock office")
        self.assertEqual(self.eng.db.recent_log(1)[0]["user"], "telegram:42/?")
        b.handle(42, "/lock office", "bad\x00\nname" + "x" * 60)
        user = self.eng.db.recent_log(1)[0]["user"]
        self.assertTrue(user.startswith("telegram:42/badname"))
        self.assertLessEqual(len(user.split("/", 1)[1]), 40)
        u = update(2, "/lock office", now=self.clock())
        del u["message"]["from"]["username"]
        u["message"]["from"]["first_name"] = "Kolt"
        b.process_update(u)
        self.assertEqual(self.eng.db.recent_log(1)[0]["user"], "telegram:42/Kolt")


if __name__ == "__main__":
    unittest.main()
