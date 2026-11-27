"""Web dashboard + JSON API (stdlib http.server, password via HTTP Basic auth)."""
import base64
import hmac
import json
import logging
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import DoorError, validate_schedule
from .unifi import UnifiError

log = logging.getLogger("doorcontrol")
STATIC = os.path.join(os.path.dirname(__file__), "static")


def make_server(cfg, engine):
    password = cfg.get("password", "")

    class Handler(BaseHTTPRequestHandler):
        server_version = "DoorControl"

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        # ---- helpers ----
        def _send(self, code, obj=None, ctype="application/json", raw=None):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    given = base64.b64decode(h[6:]).decode().partition(":")[2]
                    if hmac.compare_digest(given.encode(), password.encode()):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Door Control"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ValueError("Request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _route(self, method):
            if not self._authed():
                return
            path = self.path.split("?")[0]
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as f:
                        return self._send(200, ctype="text/html; charset=utf-8", raw=f.read())
                if method == "GET" and path == "/api/doors":
                    return self._send(200, engine.status())
                m = re.fullmatch(r"/api/doors/([^/]+)/action", path)
                if method == "POST" and m:
                    b = self._body()
                    door = engine.find_door(m.group(1))
                    desc = engine.door_action(door, b.get("action"), b.get("minutes"), source="web")
                    return self._send(200, {"ok": True, "message": "%s: %s" % (door["name"], desc)})
                if method == "GET" and path == "/api/schedules":
                    return self._send(200, engine.db.schedules())
                if method == "POST" and path == "/api/schedules":
                    sid = engine.db.add_schedule(validate_schedule(self._body()))
                    self._tick_soon()
                    return self._send(201, {"id": sid})
                m = re.fullmatch(r"/api/schedules/(\d+)", path)
                if m:
                    sid = int(m.group(1))
                    if method == "PUT":
                        if not engine.db.update_schedule(sid, validate_schedule(self._body())):
                            return self._send(404, {"error": "No such schedule"})
                        self._tick_soon()
                        return self._send(200, {"ok": True})
                    if method == "DELETE":
                        if not engine.db.delete_schedule(sid):
                            return self._send(404, {"error": "No such schedule"})
                        self._tick_soon()
                        return self._send(200, {"ok": True})
                if method == "GET" and path == "/api/upcoming":
                    return self._send(200, engine.upcoming(7))
                if method == "GET" and path == "/api/log":
                    rows = engine.db.recent_log(40)
                    for r in rows:
                        r["time"] = datetime.fromtimestamp(r["ts"], engine.tz).strftime("%a %b %d %I:%M:%S %p")
                    return self._send(200, rows)
                if method == "GET" and path == "/api/health":
                    cal = engine.calendar
                    return self._send(200, {
                        "scheduler_last_tick": engine.last_tick, "scheduler_error": engine.last_error,
                        "calendar_enabled": bool(cal and cal.url),
                        "calendar_last_ok": cal.last_ok if cal else None,
                        "calendar_error": cal.last_error if cal else None,
                    })
                return self._send(404, {"error": "Not found"})
            except DoorError as e:
                self._send(404, {"error": str(e)})
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except UnifiError as e:
                self._send(502, {"error": str(e)})
            except Exception as e:
                log.exception("web error")
                self._send(500, {"error": "Internal error: %s" % e})

        def _tick_soon(self):
            import threading
            threading.Thread(target=lambda: self._safe_tick(), daemon=True).start()

        def _safe_tick(self):
            try:
                engine.tick()
            except Exception as e:
                log.warning("tick after change failed: %s", e)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

    return ThreadingHTTPServer((cfg.get("bind", "0.0.0.0"), int(cfg.get("port", 8480))), Handler)
