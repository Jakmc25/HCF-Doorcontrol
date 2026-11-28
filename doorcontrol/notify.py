"""Telegram alerts that never slow down or break door control.

`Notifier.alert()` is the only call the scheduler, calendar, bot, web and backups make. It
takes a lock, applies the dedupe rule, appends to a bounded in-memory queue and returns: no
network I/O, never raises. A separate `notify` thread sends the queue to every allowed
private Telegram chat, at most `max_per_minute` alerts per rolling minute (extras are folded
into one summary), retrying with backoff while Telegram is down.

This module never imports web, auth or google_signin.
"""

import collections
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("doorcontrol")

# Every alert kind can be switched off in config.json (alerts.kinds.<kind>: false); a
# switched-off kind is only written to the server log.
KINDS = (
    "door_error",
    "scheduler",
    "calendar",
    "backup",
    "schedule_change",
    "status",
    "hand_change",
    "people",
    "recovered",
)
# Low priority: dropped first when the queue is full.
LOW = {"status", "schedule_change"}
SEND_URL = "https://api.telegram.org/bot%s/sendMessage"
MAX_TEXT = 3500


def telegram_send(token, chat_id, text, timeout=10):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(SEND_URL % token, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read() or b"{}")
    if not res.get("ok"):
        raise RuntimeError("Telegram did not accept the message")


def _hm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


class Notifier:
    def __init__(
        self,
        token="",
        chat_ids=(),
        cfg=None,
        send=None,
        clock=time.time,
        secrets=(),
    ):
        cfg = cfg or {}
        self.token = str(token or "")
        ids = []
        for x in chat_ids or ():
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                pass
        # Alerts go to private chats only (positive ids); group/channel ids are skipped.
        self.chats = [c for c in ids if c > 0]
        skipped = len(ids) - len(self.chats)
        if skipped and self.token:
            log.warning(
                "alerts: %d group or channel id(s) in allowed_chat_ids skipped; "
                "alerts go to private chats only.",
                skipped,
            )
        self.enabled = bool(self.token and self.chats)
        kinds = cfg.get("kinds") or {}
        self.kinds = {k: bool(kinds.get(k, True)) for k in KINDS}
        self.repeat = max(0.0, float(cfg.get("repeat_minutes", 30))) * 60
        self.max_per_minute = max(1, int(cfg.get("max_per_minute", 20)))
        self.queue_size = max(1, int(cfg.get("queue_size", 200)))
        self._send = send or (lambda chat, text: telegram_send(self.token, chat, text))
        self.clock = clock
        self._secrets = sorted(
            {str(s) for s in [self.token, *secrets] if s and len(str(s)) >= 6},
            key=len,
            reverse=True,
        )
        self._q = collections.deque()
        self._cond = threading.Condition()
        self._keys = {}
        self._keys_pruned = 0.0
        self._sent_times = collections.deque()
        self._capped = False
        self._outages = {}
        self._retry_at = 0.0
        self._backoff = 0.0
        self._unreported_drops = 0
        self.dropped_low = 0
        self.dropped_high = 0
        self.sent = 0
        self._stop = threading.Event()
        self._thread = None

    # ---- text safety ----
    def scrub(self, text):
        """Tokens and calendar addresses never leave in an alert (texts are fixed
        templates anyway; this is the backstop)."""
        t = str(text)
        for s in self._secrets:
            t = t.replace(s, "[hidden]")
        t = re.sub(r"https?://\S*calendar\S*", "[hidden]", t, flags=re.I)
        t = re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot[hidden]", t)
        return t[:MAX_TEXT]

    # ---- the one call everybody makes ----
    def alert(self, kind, text, key=None):
        """Queue an alert. O(1), no I/O, never raises. Returns True if queued to send."""
        try:
            return self._alert(kind, text, key)
        except Exception:
            log.exception("alerts: could not queue an alert")
            return False

    def _full_key(self, kind, key):
        return (kind,) + (tuple(key) if isinstance(key, (tuple, list)) else (key,))

    def _alert(self, kind, text, key):
        text = self.scrub(text)
        now = self.clock()
        with self._cond:
            if key is not None:
                k = self._full_key(kind, key)
                ent = self._keys.get(k)
                if ent and now - ent["sent"] < self.repeat:
                    ent["count"] += 1
                    ent["seen"] = now
                    return False
                if ent and ent["count"]:
                    text += " (repeated %d times since %s)" % (
                        ent["count"],
                        _hm(ent["sent"]),
                    )
                self._keys[k] = {"sent": now, "count": 0, "seen": now}
                self._prune_keys(now)
            level = logging.INFO if kind in LOW else logging.WARNING
            log.log(level, "alert [%s]: %s", kind, text)
            if not self.enabled or not self.kinds.get(kind, True):
                return False
            if len(self._q) >= self.queue_size:
                self._drop_one()
            self._q.append({"kind": kind, "text": text, "chats": None})
            self._cond.notify()
            return True

    def _prune_keys(self, now):
        if now - self._keys_pruned < 600:
            return
        self._keys_pruned = now
        for k in [k for k, v in self._keys.items() if now - v["seen"] > 86400]:
            del self._keys[k]

    def _drop_one(self):
        for i, item in enumerate(self._q):
            if item["kind"] in LOW:
                del self._q[i]
                self.dropped_low += 1
                self._unreported_drops += 1
                return
        item = self._q.popleft()
        self.dropped_high += 1
        self._unreported_drops += 1
        log.warning("alerts: queue full, dropped an alert: %s", item["text"])

    # ---- outages: one alert when it starts, one "recovered" when it ends ----
    def outage(self, name, kind, text, key=None):
        k = key if key is not None else (name, "outage")
        with self._cond:
            self._outages[name] = self._full_key(kind, k)
        return self.alert(kind, text, key=k)

    def recovered(self, name, text):
        with self._cond:
            k = self._outages.pop(name, None)
            if k is None:
                return False
            # The next outage alerts at once again.
            self._keys.pop(k, None)
        return self.alert("recovered", text)

    def in_outage(self, name):
        with self._cond:
            return name in self._outages

    # ---- sending ----
    def _summary(self, items):
        lines = [i["text"].splitlines()[0][:200] for i in items]
        text = (
            "%d more alerts (sending was paused to stay under Telegram's limit):\n"
            % (len(items))
            + "\n".join("- " + line for line in lines)
        )
        return text[:MAX_TEXT]

    def process(self):
        """Send what is due. Returns 0 to call again at once, a number of seconds to
        wait, or None when the queue is empty."""
        now = self.clock()
        with self._cond:
            if not self._q:
                return None
            if now < self._retry_at:
                return self._retry_at - now
            while self._sent_times and now - self._sent_times[0] >= 60:
                self._sent_times.popleft()
            if len(self._sent_times) >= self.max_per_minute:
                self._capped = True
                return max(0.05, 60 - (now - self._sent_times[0]))
            if self._capped and len(self._q) > 1:
                items = list(self._q)
                self._q.clear()
                item = {
                    "kind": "summary",
                    "text": self._summary(items),
                    "chats": None,
                }
            else:
                item = self._q.popleft()
            self._capped = False
            if self._unreported_drops and item["chats"] is None:
                item["text"] = (
                    item["text"]
                    + "\n(%d older alerts were dropped: too many at once or Telegram "
                    "was unreachable. See the server log.)" % self._unreported_drops
                )[:MAX_TEXT]
                self._unreported_drops = 0
        ok = self._deliver(item)
        with self._cond:
            if ok:
                self._sent_times.append(self.clock())
                self.sent += 1
                self._backoff = 0.0
            else:
                self._q.appendleft(item)
                self._backoff = min(300.0, max(5.0, self._backoff * 2))
                self._retry_at = self.clock() + self._backoff
        return 0

    def _deliver(self, item):
        chats = item["chats"] if item["chats"] is not None else list(self.chats)
        left = []
        for chat in chats:
            try:
                self._send(chat, item["text"])
            except Exception as e:
                log.warning(
                    "alerts: Telegram send failed (%s: %s); will retry",
                    type(e).__name__,
                    self.scrub(e),
                )
                left.append(chat)
        item["chats"] = left
        return not left

    def start(self):
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="notify")
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def _run(self):
        while not self._stop.is_set():
            try:
                wait = self.process()
            except Exception:
                log.exception("alerts: sender error")
                wait = 5
            if wait == 0:
                continue
            with self._cond:
                if self._stop.is_set():
                    return
                if wait is None:
                    if not self._q:
                        self._cond.wait(60)
                else:
                    self._cond.wait(wait)

    def stats(self):
        with self._cond:
            return {
                "enabled": self.enabled,
                "queued": len(self._q),
                "sent": self.sent,
                "dropped_low": self.dropped_low,
                "dropped_high": self.dropped_high,
            }
