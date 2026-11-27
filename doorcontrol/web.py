"""Web dashboard + JSON API (stdlib http.server).

Sign-in is the only gate: Google (verified hardincountyfair.org accounts) at the public
hostname, or the break-glass admin password made with manage.py. Every route except the
sign-in page, its static files, the sign-in routes and the loopback /health needs a session,
on the fair network too. Changes also need the CSRF token from /api/bootstrap.
The scheduler and the Telegram bot never depend on anything in this module.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import auth, google_signin
from .engine import DoorError, validate_schedule
from .unifi import UnifiError

log = logging.getLogger("doorcontrol")
STATIC = Path(__file__).resolve().parent / "static"
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.is_file() else "dev"

# Session lifetime for Google and admin sign-ins, in one place (copied modules read DAYS).
SESSION_DAYS = 14
AUTH_COOKIE = "doors_auth"
CSRF_COOKIE = "doors_session"
CSRF_MAX_AGE = 86400
# The only static files served without a session: what the sign-in and error pages need.
LOGIN_STATIC = {"login.js", "style.css", "fair-logo.png"}
CT = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
}
HTML = "text/html; charset=utf-8"
# Sent on every response (as Awards): no inline script or style, never framed.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
}
SERVER_ERROR = "The request could not be completed. Nothing was changed."

# Every route that needs a session: (method, path pattern, handler). Routing goes only
# through this table, so a new route cannot skip the sign-in check.
ROUTES = [
    ("GET", r"/api/bootstrap", "bootstrap"),
    ("POST", r"/api/logout", "logout"),
    ("GET", r"/api/doors", "doors"),
    ("POST", r"/api/doors/([^/]+)/action", "door_action"),
    ("GET", r"/api/schedules", "schedules"),
    ("POST", r"/api/schedules", "add_schedule"),
    ("PUT", r"/api/schedules/(\d+)", "edit_schedule"),
    ("DELETE", r"/api/schedules/(\d+)", "delete_schedule"),
    ("GET", r"/api/upcoming", "upcoming"),
    ("GET", r"/api/log", "activity"),
    ("GET", r"/api/health", "health"),
]
# Open without a session (they are how you get one).
SIGNIN_ROUTES = [
    ("GET", "/api/login-info"),
    ("POST", "/api/login"),
    ("GET", "/api/auth/google/start"),
    ("GET", google_signin.CALLBACK),
]


class SignInRequired(Exception):
    pass


class TooManyTries(Exception):
    pass


def is_loopback(address):
    return address in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def host_list(var):
    """Comma-separated host[:port] list from the environment. Empty means none."""
    return [h.strip().lower() for h in os.environ.get(var, "").split(",") if h.strip()]


def load_secret(data_dir):
    """Signs sessions and CSRF tokens. Deleting the file (and restarting) signs everyone out."""
    p = Path(data_dir) / "session-secret"
    if not p.exists():
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    return p.read_bytes()


def make_server(engine, data_dir, bind="0.0.0.0", port=8480):
    os.makedirs(data_dir, exist_ok=True)
    google_signin.DAYS = auth.DAYS = SESSION_DAYS
    secret = load_secret(data_dir)
    httpd = ThreadingHTTPServer((bind, int(port)), Handler)
    httpd.engine = engine
    httpd.secret = secret
    httpd.auth = auth.Auth(data_dir, secret, AUTH_COOKIE)
    httpd.google = google_signin.Google(secret, AUTH_COOKIE)
    httpd.public_hosts = host_list("PUBLIC_HOSTS")
    httpd.allowed_hosts = host_list("ALLOWED_HOSTS")
    if not httpd.google.ready():
        log.warning("Google sign-in is not set up (GOOGLE_* settings in .env).")
    if not httpd.public_hosts:
        log.warning("PUBLIC_HOSTS is empty: nobody can sign in.")
    return httpd


class Handler(BaseHTTPRequestHandler):
    server_version = "DoorControl"

    def log_message(self, fmt, *args):
        log.debug("web: " + fmt, *args)

    # ---- plumbing ----
    @property
    def engine(self):
        return self.server.engine

    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def send(self, code, data=None, ctype="application/json", extra=None):
        if ctype == "application/json":
            body = json.dumps(data).encode()
        else:
            body = data.encode() if isinstance(data, str) else (data or b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def static(self, name):
        root = STATIC
        f = (root / name).resolve()
        if f.parent != root or not f.is_file():
            raise LookupError("Not found.")
        return self.send(
            200, f.read_bytes(), CT.get(f.suffix, "application/octet-stream")
        )

    def jar(self, name):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            return jar[name].value
        except Exception:
            return ""

    def sign(self, v):
        return hmac.new(self.server.secret, v.encode(), hashlib.sha256).hexdigest()

    def ip(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def body(self, limit=65536):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Request too large.")
        b = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(b, dict):
            raise ValueError("Invalid request.")
        return b

    # ---- CSRF ----
    def csrf_cookie(self):
        """The signed CSRF cookie value if valid, else ''. Format: stamp.nonce.mac"""
        try:
            v = self.jar(CSRF_COOKIE)
            stamp, nonce, mac = v.split(".")
            if abs(time.time() - int(stamp)) < CSRF_MAX_AGE and hmac.compare_digest(
                self.sign(stamp + "." + nonce), mac
            ):
                return v
        except Exception:
            pass
        return ""

    def same_origin(self):
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            raise PermissionError("Request origin does not match this application.")

    def check_csrf(self):
        cookie = self.csrf_cookie()
        if not cookie or not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", ""), self.sign(cookie)
        ):
            raise PermissionError("Reload the page and try again.")
        self.same_origin()

    # ---- entry points ----
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        self.user = None
        try:
            self.route(method)
        except SignInRequired:
            self.send(401, {"error": "Sign in to continue.", "signin": True})
        except TooManyTries:
            self.send(429, {"error": "Too many tries. Wait 15 minutes and try again."})
        except DoorError as e:
            self.send(404, {"error": str(e)})
        except UnifiError as e:
            self.send(502, {"error": str(e)})
        except ValueError as e:  # includes bad JSON
            self.send(400, {"error": str(e) or "Invalid request."})
        except PermissionError as e:
            self.send(403, {"error": str(e)})
        except LookupError as e:
            self.send(404, {"error": str(e) or "Not found."})
        except Exception:
            log.exception("web error on %s %s", method, self.path.split("?")[0])
            self.send(500, {"error": SERVER_ERROR})

    def route(self, method):
        host = (self.headers.get("Host") or "").lower()
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)
        cloudflare = bool(self.headers.get("CF-Connecting-IP"))

        # Docker's health check comes from inside the container: no Host rules, no sign-in.
        if path == "/health":
            if (
                method == "GET"
                and is_loopback(self.client_address[0])
                and not cloudflare
            ):
                return self.send(200, {"ok": True, "version": VERSION})
            raise LookupError("Not found.")

        public = self.server.public_hosts
        self.remote = host in public or cloudflare
        if self.remote:
            # Anything that came through Cloudflare must use the public hostname.
            if host not in public:
                raise LookupError("Not found.")
        elif host not in self.server.allowed_hosts:
            # Empty ALLOWED_HOSTS refuses every fair-network address.
            raise PermissionError("Use the fair server address to open Door Control.")

        # ---- sign-in routes (no session needed) ----
        if method == "GET" and path == "/api/login-info":
            return self.login_info()
        if method == "GET" and path in (
            "/api/auth/google/start",
            google_signin.CALLBACK,
        ):
            if not self.remote:
                # Google only returns to the HTTPS public hostname.
                raise LookupError("Not found.")
            return self.google(host, path, q)
        if method == "POST" and path == "/api/login":
            return self.password_login()

        # ---- the gate ----
        cookie = self.jar(AUTH_COOKIE)
        self.user = (
            self.server.auth.verify(cookie) or self.server.google.verify(cookie)
            if cookie
            else None
        )
        if not self.user:
            if method == "GET" and path in ("/", "/index.html"):
                return self.static("login.html")
            if (
                method == "GET"
                and path.startswith("/static/")
                and path[len("/static/") :] in LOGIN_STATIC
            ):
                return self.static(path[len("/static/") :])
            raise SignInRequired()

        if method == "GET" and path in ("/", "/index.html"):
            return self.static("index.html")
        if method == "GET" and path.startswith("/static/"):
            return self.static(path[len("/static/") :])
        for m, pattern, name in ROUTES:
            match = re.fullmatch(pattern, path)
            if match and m == method:
                if method != "GET":
                    self.check_csrf()
                return getattr(self, "api_" + name)(*match.groups())
        raise LookupError("Not found.")

    # ---- sign-in ----
    def google_url(self):
        if self.remote:
            return "/api/auth/google/start"
        public = self.server.public_hosts
        # On the fair network the button goes to the public hostname (HTTPS).
        return f"https://{public[0]}/api/auth/google/start" if public else ""

    def login_info(self):
        url = self.google_url()
        return self.send(
            200,
            {
                "app": "Fairgrounds Door Control",
                "google": self.server.google.ready() and bool(url),
                "google_url": url,
                "remote": self.remote,
                "password": bool(self.server.auth.load()),
            },
        )

    def google(self, host, path, q):
        g = self.server.google
        if not g.ready():
            return self.send(
                404,
                google_signin.page(
                    "Google sign-in isn’t set up on this app yet. Ask Jake."
                ),
                HTML,
            )
        if path.endswith("/start"):
            url, cookie = g.start(host)
            return self.send(302, b"", HTML, {"Location": url, "Set-Cookie": cookie})
        try:
            email = g.finish(
                host,
                {k: v[0] for k, v in q.items()},
                self.jar(google_signin.STATE_COOKIE),
            )
        except PermissionError as e:
            log.warning("google sign-in refused: %s (%s)", e, self.ip())
            return self.send(403, google_signin.page(str(e)), HTML)
        log.info("signed in with Google: %s (%s)", email, self.ip())
        self.engine.db.log("", "sign-in", "web", True, "signed in", user=email)
        return self.send(
            200,
            google_signin.page(),
            HTML,
            {"Set-Cookie": g.cookie_header(g.issue(email))},
        )

    def password_login(self):
        """Break-glass admin sign-in. Only at the public hostname (the cookie is Secure)."""
        self.same_origin()
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("Use a JSON request.")
        if not self.remote:
            raise PermissionError("Admin sign-in works only at the internet address.")
        b = self.body(4000)
        ip = self.ip()
        a = self.server.auth
        if not a.allowed(ip):
            raise TooManyTries()
        name = a.check(b.get("name"), b.get("password")) if a.load() else None
        if not name:
            a.failed(ip)
            log.warning("admin sign-in failed (%s)", ip)
            return self.send(401, {"error": "Wrong name or password."})
        log.info("admin sign-in: %s (%s)", name, ip)
        self.engine.db.log(
            "", "sign-in", "web", True, "signed in (password)", user=name
        )
        return self.send(
            200, {"ok": True}, extra={"Set-Cookie": a.cookie_header(a.issue(name))}
        )

    # ---- API (signed in) ----
    def api_bootstrap(self):
        cookie = self.csrf_cookie()
        # Reuse a fresh token; renew it once it is half way to expiry.
        if not cookie or time.time() - int(cookie.split(".")[0]) > CSRF_MAX_AGE / 2:
            v = str(int(time.time())) + "." + secrets.token_hex(20)
            cookie = v + "." + self.sign(v)
        return self.send(
            200,
            {"csrf": self.sign(cookie), "user": self.user, "version": VERSION},
            extra={
                "Set-Cookie": f"{CSRF_COOKIE}={cookie}; HttpOnly; Secure; "
                f"SameSite=Strict; Path=/; Max-Age={CSRF_MAX_AGE}"
            },
        )

    def api_logout(self):
        # Sessions are stateless: this clears the browser's cookie only.
        return self.send(
            200,
            {"ok": True},
            extra={"Set-Cookie": self.server.auth.cookie_header("", clear=True)},
        )

    def api_doors(self):
        return self.send(200, self.engine.status())

    def api_door_action(self, door_query):
        b = self.body()
        door = self.engine.find_door(door_query)
        desc = self.engine.door_action(
            door, b.get("action"), b.get("minutes"), source="web", user=self.user
        )
        return self.send(200, {"ok": True, "message": "%s: %s" % (door["name"], desc)})

    def api_schedules(self):
        return self.send(200, self.engine.db.schedules())

    def note(self, action, detail):
        """Activity log row for a change made on the dashboard."""
        self.engine.db.log("", action, "web", True, detail, user=self.user)

    def api_add_schedule(self):
        s = validate_schedule(self.body())
        sid = self.engine.db.add_schedule(s)
        self.note("schedule add", 'added schedule "%s"' % s["name"])
        self.tick_soon()
        return self.send(201, {"id": sid})

    def api_edit_schedule(self, sid):
        sid = int(sid)
        old = self.engine.db.get_schedule(sid)
        if not old:
            raise LookupError("No such schedule")
        new = validate_schedule(self.body())
        if not self.engine.db.update_schedule(sid, new):
            raise LookupError("No such schedule")
        same = all(old.get(k) == v for k, v in new.items() if k != "enabled")
        if same and old["enabled"] != new["enabled"]:
            verb = "resume" if new["enabled"] else "pause"
            self.note("schedule " + verb, '%sd schedule "%s"' % (verb, new["name"]))
        elif old["name"] != new["name"]:
            self.note(
                "schedule edit",
                'changed schedule "%s" (now "%s")' % (old["name"], new["name"]),
            )
        else:
            self.note("schedule edit", 'changed schedule "%s"' % new["name"])
        self.tick_soon()
        return self.send(200, {"ok": True})

    def api_delete_schedule(self, sid):
        sid = int(sid)
        old = self.engine.db.get_schedule(sid)
        if not old or not self.engine.db.delete_schedule(sid):
            raise LookupError("No such schedule")
        self.note("schedule delete", 'deleted schedule "%s"' % old["name"])
        self.tick_soon()
        return self.send(200, {"ok": True})

    def api_upcoming(self):
        return self.send(200, self.engine.upcoming(7))

    def api_activity(self):
        rows = self.engine.db.recent_log(40)
        for r in rows:
            r["time"] = datetime.fromtimestamp(r["ts"], self.engine.tz).strftime(
                "%a %b %d %I:%M:%S %p"
            )
        return self.send(200, rows)

    def api_health(self):
        e = self.engine
        cal = e.calendar
        return self.send(
            200,
            {
                "scheduler_last_tick": e.last_tick,
                "scheduler_error": e.last_error,
                "calendar_enabled": bool(cal and cal.url),
                "calendar_last_ok": cal.last_ok if cal else None,
                "calendar_error": cal.last_error if cal else None,
            },
        )

    def tick_soon(self):
        threading.Thread(target=self._safe_tick, daemon=True).start()

    def _safe_tick(self):
        try:
            self.engine.tick()
        except Exception as e:
            log.warning("tick after change failed: %s", e)
