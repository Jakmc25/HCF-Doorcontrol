"""Fake UniFi Access controller for testing without touching real doors.

python3 mock_unifi.py 12445        # then point config.json at http://127.0.0.1:12445
"""

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "test-token"


class Mock:
    def __init__(self):
        self.doors = {
            "d1": {
                "id": "d1",
                "name": "Event Center",
                "full_name": "Fairgrounds - Event Center",
                "door_lock_relay_status": "lock",
                "door_position_status": "close",
            },
            "d2": {
                "id": "d2",
                "name": "Office",
                "full_name": "Fairgrounds - Office",
                "door_lock_relay_status": "lock",
                "door_position_status": "close",
            },
            "d3": {
                "id": "d3",
                "name": "Main Gate",
                "full_name": "Fairgrounds - Main Gate",
                "door_lock_relay_status": "lock",
                "door_position_status": "close",
            },
        }
        self.rules = {k: {"type": "schedule", "ended_time": 0} for k in self.doors}
        self.calls = []  # (method, path, body)
        self.fail = False


def make_server(mock, port=0):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, data=None, code_str="SUCCESS"):
            body = json.dumps(
                {"code": code_str, "msg": "success", "data": data}
            ).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self._reply(401, None, "CODE_UNAUTHORIZED")
            mock.calls.append((method, self.path, body))
            if mock.fail:
                return self._reply(500, None, "CODE_SYSTEM_ERROR")
            if method == "GET" and self.path == "/api/v1/developer/doors":
                return self._reply(200, list(mock.doors.values()))
            m = re.fullmatch(r"/api/v1/developer/doors/(\w+)/lock_rule", self.path)
            if m and m.group(1) in mock.doors:
                did = m.group(1)
                if method == "GET":
                    return self._reply(200, mock.rules[did])
                t = body.get("type")
                mock.rules[did] = {
                    "type": "schedule" if t == "reset" else t,
                    "ended_time": int(time.time()) + 60 * body.get("interval", 0),
                }
                mock.doors[did]["door_lock_relay_status"] = (
                    "unlock" if t in ("keep_unlock", "custom") else "lock"
                )
                return self._reply(200, "success")
            m = re.fullmatch(r"/api/v1/developer/doors/(\w+)/unlock", self.path)
            if m and method == "PUT" and m.group(1) in mock.doors:
                return self._reply(200, "success")
            return self._reply(404, None, "CODE_NOT_FOUND")

        def do_GET(self):
            self._handle("GET")

        def do_PUT(self):
            self._handle("PUT")

    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 12445
    m = Mock()
    srv = make_server(m, port)
    print("Mock UniFi Access on http://127.0.0.1:%d  (token: %s)" % (port, TOKEN))
    threading.Event().wait()
