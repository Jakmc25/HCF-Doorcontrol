"""Telegram control: long-polling bot using only the standard library.

Safety (1.1.0): the update offset is kept in the database and stored before each update is
handled, so a restart never replays a door command; messages older than max_age_seconds are
dropped without action or reply; each chat may send max_commands_per_minute commands; only
private text messages are handled (groups, channels, edits and callbacks are ignored); door
actions are logged as telegram:<chat id>/<name>.
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

from .engine import DoorError
from .unifi import UnifiError

log = logging.getLogger("doorcontrol")

HELP = (
    "Door Control commands:\n"
    "/doors - list doors and whether they're locked\n"
    "/unlock <door> [time] - unlock for a while, e.g. /unlock event center 2h (default 60m, max 8h)\n"
    "/keepunlocked <door> - stay unlocked until you /reset or /lock\n"
    "/lock <door> - lock now\n"
    "/keeplocked <door> - stay locked (ignores the door's normal schedule)\n"
    "/reset <door> - return to the door's normal UniFi schedule\n"
    "/open <door> - quick momentary unlock\n"
    "/schedules - list schedules\n"
    "/upcoming - what's scheduled in the next 3 days"
)

_DUR = re.compile(
    r"^(.*?)(?:\s+((?:\d+\s*(?:h|hr|hrs|hours?|m|mins?|minutes?)\s*)+))?$", re.I | re.S
)
_PART = re.compile(r"(\d+)\s*(h|hr|hrs|hours?|m|mins?|minutes?)", re.I)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def split_duration(text):
    """'event center 2h' -> ('event center', 120). No duration -> (text, None)."""
    m = _DUR.match(text.strip())
    name, dur = m.group(1).strip(), m.group(2)
    if not dur:
        return text.strip(), None
    mins = 0
    for n, unit in _PART.findall(dur):
        mins += int(n) * (60 if unit.lower().startswith("h") else 1)
    return name, mins


def fmt_schedule(s, names):
    if s["doors"] == ["*"] or "*" in s["doors"]:
        doors = "all doors"
    else:
        doors = ", ".join(names.get(i, "?") for i in s["doors"])
    when = s["date"] if s["date"] else ",".join(DAY_NAMES[d] for d in s["days"])
    return "#%d %s: %s %s-%s -> %s%s" % (
        s["id"],
        s["name"],
        when,
        s["start"],
        s["end"],
        doors,
        "" if s["enabled"] else " (paused)",
    )


OFFSET_KEY = "telegram_offset"
SLOW_DOWN = "Slow down: wait a minute."


def who(chat_id, name=""):
    """Activity-log user for a Telegram action: telegram:<chat id>/<name>, never empty."""
    clean = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()[:40]
    return "telegram:%s/%s" % (chat_id, clean or "?")


class Bot:
    def __init__(self, cfg, engine, api=None, clock=time.time):
        self.token = cfg.get("token", "")
        self.allowed = {int(x) for x in cfg.get("allowed_chat_ids", [])}
        self.engine = engine
        self.max_age = int(cfg.get("max_age_seconds", 120))
        self.max_per_minute = max(1, int(cfg.get("max_commands_per_minute", 10)))
        self._api_fn = api
        self.clock = clock
        self._recent = {}
        self._warned = {}
        self._stop = threading.Event()

    # ---- command handling (pure, testable) ----
    def handle(self, chat_id, text, name=""):
        text = (text or "").strip()
        if not text.startswith("/"):
            return "Send /help to see what I can do."
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        if cmd in ("/start", "/help", "/chatid") and chat_id not in self.allowed:
            return (
                "Not authorized. Your chat ID is %s - add it to allowed_chat_ids in config.json."
                % chat_id
            )
        if chat_id not in self.allowed:
            return "Not authorized. Your chat ID is %s." % chat_id
        if cmd in ("/start", "/help"):
            return HELP
        try:
            return self._run(cmd, rest.strip(), who(chat_id, name))
        except (DoorError, ValueError) as e:
            return str(e)
        except UnifiError as e:
            return "UniFi problem: %s" % e

    def _run(self, cmd, rest, user):
        e = self.engine
        if cmd == "/doors":
            lines = []
            for d in e.status():
                icon = "🔓" if d["relay"] == "unlock" else "🔒"
                extra = (
                    " [%s]" % d["rule"] if d["rule"] and d["rule"] != "schedule" else ""
                )
                lines.append("%s %s%s" % (icon, d["name"], extra))
            return "\n".join(lines) or "No doors found."
        if cmd == "/schedules":
            names = {d["id"]: d["name"] for d in e.doors()}
            rows = [fmt_schedule(s, names) for s in e.db.schedules()]
            return (
                "\n".join(rows)
                or "No schedules yet (create them on the web dashboard)."
            )
        if cmd == "/upcoming":
            rows = []
            for w in e.upcoming(3):
                a, b = datetime.fromisoformat(w["start"]), datetime.fromisoformat(
                    w["end"]
                )
                rows.append(
                    "%s%s - %s: %s (%s)"
                    % (
                        "NOW " if w["active"] else "",
                        a.strftime("%a %b %d %I:%M%p"),
                        b.strftime("%I:%M%p"),
                        w["title"],
                        ", ".join(w["doors"]),
                    )
                )
            return "\n".join(rows) or "Nothing scheduled in the next 3 days."
        table = {
            "/unlock": "unlock_for",
            "/keepunlocked": "keep_unlock",
            "/lock": "lock",
            "/keeplocked": "keep_lock",
            "/reset": "reset",
            "/open": "pulse",
        }
        if cmd not in table:
            return "Unknown command. Send /help."
        if not rest:
            raise ValueError("Which door? Example: %s front gate" % cmd)
        minutes = None
        if cmd == "/unlock":
            rest, minutes = split_duration(rest)
        door = e.find_door(rest)
        desc = e.door_action(door, table[cmd], minutes, source="telegram", user=user)
        return "%s: %s" % (door["name"], desc)

    # ---- updates (testable without a network) ----
    def _rate_limited(self, chat, now):
        """None: go ahead. Otherwise the reply to send ("" = send nothing)."""
        q = self._recent.setdefault(chat, collections.deque())
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= self.max_per_minute:
            if now - self._warned.get(chat, -1e18) >= 60:
                self._warned[chat] = now
                return SLOW_DOWN
            return ""
        q.append(now)
        return None

    def process_update(self, u):
        """One update from getUpdates -> (chat id, reply) or None (nothing to send)."""
        msg = u.get("message")
        if not isinstance(msg, dict):
            return None  # callback_query, edited_message, channel_post, ...
        chat = msg.get("chat") or {}
        text = msg.get("text")
        if chat.get("type") != "private" or not text or "id" not in chat:
            return None
        chat_id = chat["id"]
        now = self.clock()
        cmd = str(text).strip().split(" ")[0][:32]
        try:
            age = now - float(msg.get("date") or 0)
        except (TypeError, ValueError):
            age = float("inf")
        if age > self.max_age:
            log.info(
                "telegram: dropped a %d s old message from chat %s (%s)",
                min(age, 10**9),
                chat_id,
                cmd,
            )
            return None
        limited = self._rate_limited(chat_id, now)
        if limited is not None:
            log.info("telegram: chat %s is over the command limit (%s)", chat_id, cmd)
            return (chat_id, limited) if limited else None
        frm = msg.get("from") or {}
        name = frm.get("username") or frm.get("first_name") or ""
        return chat_id, self.handle(chat_id, text, name)

    def poll_once(self, timeout=25):
        db = self.engine.db
        try:
            offset = int(db.kv_get(OFFSET_KEY, 0) or 0)
        except ValueError:
            offset = 0
        res = self.api("getUpdates", {"timeout": timeout, "offset": offset})
        for u in res.get("result", []):
            # Stored BEFORE handling: a crash never runs a door command twice.
            db.kv_set(OFFSET_KEY, int(u["update_id"]) + 1)
            out = self.process_update(u)
            if out and out[1]:
                self.api("sendMessage", {"chat_id": out[0], "text": out[1]})

    # ---- network loop ----
    def api(self, method, params, timeout=40):
        if self._api_fn:
            return self._api_fn(method, params)
        return self._api(method, params, timeout)

    def _api(self, method, params, timeout=40):
        url = "https://api.telegram.org/bot%s/%s" % (self.token, method)
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=timeout
        ) as r:
            return json.loads(r.read())

    def start(self):
        if not self.token:
            return
        threading.Thread(target=self._loop, daemon=True, name="telegram").start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as ex:
                text = str(ex).replace(self.token, "[hidden]") if self.token else ex
                log.warning("telegram: %s: %s", type(ex).__name__, text)
                self._stop.wait(10)
