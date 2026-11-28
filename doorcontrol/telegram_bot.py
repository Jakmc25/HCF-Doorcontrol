"""Telegram control: long-polling bot using only the standard library."""

import json
import logging
import re
import threading
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


class Bot:
    def __init__(self, cfg, engine):
        self.token = cfg.get("token", "")
        self.allowed = {int(x) for x in cfg.get("allowed_chat_ids", [])}
        self.engine = engine
        self._stop = threading.Event()

    # ---- command handling (pure, testable) ----
    def handle(self, chat_id, text):
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
            return self._run(cmd, rest.strip())
        except (DoorError, ValueError) as e:
            return str(e)
        except UnifiError as e:
            return "UniFi problem: %s" % e

    def _run(self, cmd, rest):
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
        desc = e.door_action(door, table[cmd], minutes, source="telegram")
        return "%s: %s" % (door["name"], desc)

    # ---- network loop ----
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
        offset = 0
        while not self._stop.is_set():
            try:
                res = self._api("getUpdates", {"timeout": 25, "offset": offset})
                for u in res.get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if msg.get("text") and msg.get("chat", {}).get("type") == "private":
                        reply = self.handle(msg["chat"]["id"], msg["text"])
                        self._api(
                            "sendMessage", {"chat_id": msg["chat"]["id"], "text": reply}
                        )
            except Exception as ex:
                log.warning("telegram: %s", ex)
                self._stop.wait(10)
