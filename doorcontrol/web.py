"""Web dashboard + JSON API (stdlib http.server).

Sign-in is the only gate: Google at the public hostname (the admins in
GOOGLE_ALLOWED_EMAILS plus the in-app People list), or the break-glass admin password made
with manage.py. Every route except the sign-in page, its static files, the sign-in routes
and the loopback /health needs a session, on the fair network too. Changes also need the
CSRF token from /api/bootstrap.

Sessions (1.2.0) are server-side: the browser holds a random id, the database holds only
its SHA-256, and every request checks that the person is still allowed. 12 hours; sign
out, `manage.py revoke`, removal from People or from .env (after a restart) end them.
The scheduler and the Telegram bot never depend on anything in this module.
"""

import hashlib
import hmac
import ipaddress
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

from . import auth, google_signin, people
from .engine import DoorError, validate_schedule
from .unifi import UnifiError

log = logging.getLogger("doorcontrol")
STATIC = Path(__file__).resolve().parent / "static"
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.is_file() else "dev"

# Session lifetime for Google and admin sign-ins, in one place (server-side sessions).
SESSION_HOURS = 12
SESSION_COOKIE = "__Host-doors_sid"
# The 1.0.0/1.1.0 stateless cookie: never read again, cleared when a browser sends it.
OLD_COOKIE = "doors_auth"
CSRF_COOKIE = "doors_session"
PURGE_SECONDS = 3600
MAX_PEOPLE = 500
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
# Only these TCP peers may vouch for a request through CF-Connecting-IP and the public Host.
# Default: the gateway of the app's own compose network (door-control-net, 172.31.248.0/29),
# which is where cloudflared on fair-server appears when it connects to 127.0.0.1:8480.
# Other containers (other fair apps) are on other networks and are not trusted. Loopback is
# left out: inside the container only Docker's health check uses it, and /health needs no
# trust. Wrong value = public sign-in refused, never open. X-Forwarded-* headers are ignored.
DEFAULT_TRUSTED_PROXIES = "172.31.248.1/32"
MAX_CONNECTIONS = 64

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
    ("GET", r"/api/people", "people"),
    ("POST", r"/api/people", "add_person"),
    ("POST", r"/api/people/remove", "remove_person"),
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


class DoorGoogle(google_signin.Google):
    """The shared Google sign-in (pinned, never edited here) with this app's list of who
    may sign in: the admins in GOOGLE_ALLOWED_EMAILS plus the in-app People list.

    The parent's __init__ assigns `self.emails` from the environment; the setter keeps
    that as `admins`. Every read of `emails` (the parent's finish(), ready(), start() and
    allowed()) gets the admins plus the people list from the database at that moment.
    GOOGLE_ALLOWED_DOMAINS still works as in the parent (empty on fair-server).
    """

    def __init__(self, secret, cookie, db):
        self._db = db
        self.admins = []
        super().__init__(secret, cookie)

    @property
    def emails(self):
        return sorted(set(self.admins) | people.listed(self._db))

    @emails.setter
    def emails(self, value):
        self.admins = sorted({str(e).strip().lower() for e in value if str(e).strip()})

    def is_admin(self, email):
        return str(email or "").lower() in self.admins


class Sessions:
    """Server-side sessions. The cookie holds a random 32-byte id; the database keeps only
    sha256(id), who, kind (google|admin), the account generation for admin sign-ins,
    created, expires and the address. Expired rows are purged at most once an hour."""

    def __init__(self, db, hours=SESSION_HOURS, clock=time.time):
        self.db, self.hours, self.clock = db, hours, clock
        self._purged = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def digest(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    def create(self, user, kind, gen=None, ip=""):
        sid = secrets.token_urlsafe(32)
        now = self.clock()
        self.db.session_add(
            self.digest(sid), user, kind, gen, now, now + self.hours * 3600, ip
        )
        return sid

    def purge(self):
        now = self.clock()
        with self._lock:
            if now - self._purged < PURGE_SECONDS:
                return
            self._purged = now
        self.db.sessions_purge(now)

    def lookup(self, sid):
        self.purge()
        if not sid or len(sid) > 200:
            return None
        h = self.digest(sid)
        row = self.db.session_get(h)
        if row and row["expires"] <= self.clock():
            self.db.session_delete(h)
            return None
        return row

    def delete(self, sid):
        if sid:
            self.db.session_delete(self.digest(sid))

    def cookie(self, sid):
        return (
            f"{SESSION_COOKIE}={sid}; HttpOnly; Secure; SameSite=Strict; Path=/; "
            f"Max-Age={int(self.hours * 3600)}"
        )

    @staticmethod
    def clear(name=SESSION_COOKIE):
        return f"{name}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"


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


def proxy_list(value):
    """'127.0.0.1, 172.16.0.0/12' -> networks. A bad entry stops the app (fail closed)."""
    nets = []
    for item in str(value or "").split(","):
        if item.strip():
            try:
                nets.append(ipaddress.ip_network(item.strip(), strict=False))
            except ValueError:
                raise SystemExit(
                    "TRUSTED_PROXIES: %r is not an IP address or range." % item
                )
    return nets


def peer_in(address, networks):
    try:
        a = ipaddress.ip_address(address)
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a.version == n.version and a in n for n in networks)


class FailureLimiter:
    """Break-glass failures: at most `per_address` per address and `overall` in total per
    window (15 minutes). Old entries are pruned on every call, so memory stays bounded.
    """

    def __init__(self, per_address=10, overall=50, window=900):
        self.per_address, self.overall, self.window = per_address, overall, window
        self.by_address = {}
        self.lock = threading.Lock()

    def _prune(self, now):
        for ip in list(self.by_address):
            kept = [t for t in self.by_address[ip] if now - t < self.window]
            if kept:
                self.by_address[ip] = kept
            else:
                del self.by_address[ip]

    def total(self):
        return sum(len(v) for v in self.by_address.values())

    def allowed(self, ip):
        with self.lock:
            self._prune(time.time())
            return (
                len(self.by_address.get(ip, ())) < self.per_address
                and self.total() < self.overall
            )

    def failed(self, ip):
        with self.lock:
            now = time.time()
            self._prune(now)
            if self.total() < self.overall:
                self.by_address.setdefault(ip, []).append(now)


class Server(ThreadingHTTPServer):
    """At most `max_connections` requests at once; extra connections are closed at once."""

    daemon_threads = True

    def __init__(self, address, handler, max_connections=MAX_CONNECTIONS):
        super().__init__(address, handler)
        self.max_connections = max_connections
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(
    engine, data_dir, bind="0.0.0.0", port=8480, max_connections=MAX_CONNECTIONS
):
    os.makedirs(data_dir, exist_ok=True)
    secret = load_secret(data_dir)
    httpd = Server((bind, int(port)), Handler, max_connections)
    httpd.trusted_proxies = proxy_list(
        os.environ.get("TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES)
    )
    httpd.limiter = FailureLimiter()
    httpd.engine = engine
    httpd.secret = secret
    httpd.auth = auth.Auth(data_dir, secret, SESSION_COOKIE)
    httpd.google = DoorGoogle(secret, SESSION_COOKIE, engine.db)
    httpd.sessions = Sessions(engine.db)
    httpd.public_hosts = host_list("PUBLIC_HOSTS")
    httpd.allowed_hosts = host_list("ALLOWED_HOSTS")
    if not httpd.google.ready():
        log.warning(
            "Google sign-in is not set up or nobody may use it (GOOGLE_* settings "
            "in .env, People list)."
        )
    if httpd.google.domains:
        log.warning(
            "GOOGLE_ALLOWED_DOMAINS is set: every account on %s may sign in.",
            ", ".join(httpd.google.domains),
        )
    if not httpd.public_hosts:
        log.warning("PUBLIC_HOSTS is empty: nobody can sign in.")
    return httpd


class Handler(BaseHTTPRequestHandler):
    server_version = "DoorControl"
    timeout = 30  # seconds a connection may sit idle or half-sent

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
        for c in getattr(self, "clear_cookies", ()):
            self.send_header("Set-Cookie", c)
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

    def from_proxy(self):
        return peer_in(self.client_address[0], self.server.trusted_proxies)

    def ip(self):
        """The person's address: CF-Connecting-IP only when a trusted proxy sent it."""
        if self.from_proxy():
            return self.headers.get("CF-Connecting-IP") or self.client_address[0]
        return self.client_address[0]

    def body(self, limit=65536):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Invalid request length.")
        if n < 0:
            raise ValueError("Invalid request length.")
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

    def not_allowed(self, head=False):
        body = json.dumps({"error": "This kind of request is not supported."}).encode()
        self.send_response(405)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Allow", "GET, POST, PUT, DELETE")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_HEAD(self):
        self.not_allowed(head=True)

    def do_PATCH(self):
        self.not_allowed()

    def do_OPTIONS(self):
        self.not_allowed()

    def send_error(self, code, message=None, explain=None):
        """Malformed or unsupported requests: a plain JSON sentence, with the security headers."""
        self.close_connection = True
        text = {
            400: "The request could not be read.",
            408: "The request took too long.",
            414: "The address is too long.",
            431: "The request headers are too large.",
            501: "This kind of request is not supported.",
        }.get(code, "The request could not be handled.")
        body = json.dumps({"error": text}).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD" and code >= 200 and code not in (204, 304):
                self.wfile.write(body)
        except OSError:
            pass

    def dispatch(self, method):
        self.user = None
        self.session = None
        self.clear_cookies = []
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
        # Headers that claim Cloudflare count only from a trusted proxy (TRUSTED_PROXIES).
        proxy = self.from_proxy()
        cloudflare = proxy and bool(self.headers.get("CF-Connecting-IP"))

        # Docker's health check comes from inside the container: no Host rules, no sign-in.
        if path == "/health":
            if (
                method == "GET"
                and is_loopback(self.client_address[0])
                and not self.headers.get("CF-Connecting-IP")
            ):
                # 503 when the scheduler stalled (Docker's HEALTHCHECK then reports
                # unhealthy). Nothing else is shown here.
                ok = bool(self.engine.healthy())
                return self.send(200 if ok else 503, {"ok": ok, "version": VERSION})
            raise LookupError("Not found.")

        public = self.server.public_hosts
        self.remote = proxy and (host in public or cloudflare)
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
        if self.jar(OLD_COOKIE):
            # 1.1.0 and older stateless cookies are never accepted; clear them.
            self.clear_cookies.append(Sessions.clear(OLD_COOKIE))
        self.session = self.current_session()
        # Break-glass sessions are shown and logged as "admin:NAME", never like an email.
        self.user = self.session["user"] if self.session else None
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

    # ---- sessions ----
    def current_session(self):
        """The session row for this request's cookie if it exists, has not expired and
        its person is still allowed; otherwise None (and the row is deleted)."""
        sessions = self.server.sessions
        sessions.purge()  # expired rows go at most once an hour
        sid = self.jar(SESSION_COOKIE)
        if not sid:
            return None
        row = sessions.lookup(sid)
        if not row:
            self.clear_cookies.append(Sessions.clear())
            return None
        if not self.still_allowed(row):
            sessions.delete(sid)
            self.clear_cookies.append(Sessions.clear())
            log.info("session ended: %s is no longer allowed", row["user"])
            return None
        return row

    def still_allowed(self, row):
        if row["kind"] == "google":
            return self.server.google.allowed(row["user"])
        if row["kind"] == "admin" and row["user"].startswith("admin:"):
            acct = self.server.auth.load().get(row["user"][len("admin:") :])
            # set-password gives a new gen and remove-user deletes the account.
            return bool(
                acct and row["gen"] and hmac.compare_digest(acct["gen"], row["gen"])
            )
        return False

    def is_admin(self):
        """Admins manage the People list: a Google session whose email is in
        GOOGLE_ALLOWED_EMAILS. Break-glass admin:NAME is not an admin for this."""
        s = self.session
        return bool(
            s and s["kind"] == "google" and self.server.google.is_admin(s["user"])
        )

    def require_admin(self):
        if not self.is_admin():
            raise PermissionError("Only admins can see or change the People list.")

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
            # AC41: refusals (for example a fair account not on the list) are logged.
            self.engine.db.log(
                "",
                "sign-in refused",
                "web",
                False,
                str(e)[:300],
                kind="signin",
            )
            return self.send(403, google_signin.page(str(e)), HTML)
        log.info("signed in with Google: %s (%s)", email, self.ip())
        self.engine.db.log(
            "", "sign-in", "web", True, "signed in", user=email, kind="signin"
        )
        sid = self.server.sessions.create(email, "google", ip=self.ip())
        return self.send(
            200,
            google_signin.page(),
            HTML,
            {"Set-Cookie": self.server.sessions.cookie(sid)},
        )

    def password_login(self):
        """Break-glass admin sign-in. Only at the public hostname (the cookie is Secure)."""
        self.same_origin()
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("Use a JSON request.")
        ip = self.ip()
        limiter = self.server.limiter
        if not limiter.allowed(ip):
            log.warning("admin sign-in refused: too many failures (%s)", ip)
            raise TooManyTries()
        if not self.remote:
            limiter.failed(ip)
            log.warning("admin sign-in refused: not the public address (%s)", ip)
            raise PermissionError("Admin sign-in works only at the internet address.")
        b = self.body(4000)
        a = self.server.auth
        name = a.check(b.get("name"), b.get("password")) if a.load() else None
        if not name:
            limiter.failed(ip)
            log.warning("admin sign-in failed (%s)", ip)
            return self.send(401, {"error": "Wrong name or password."})
        log.info("admin sign-in: admin:%s (%s)", name, ip)
        self.engine.db.log(
            "",
            "sign-in",
            "web",
            True,
            "signed in (password)",
            user="admin:" + name,
            kind="signin",
        )
        gen = a.load()[name]["gen"]
        sid = self.server.sessions.create("admin:" + name, "admin", gen, ip)
        return self.send(
            200, {"ok": True}, extra={"Set-Cookie": self.server.sessions.cookie(sid)}
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
            {
                "csrf": self.sign(cookie),
                "user": self.user,
                "admin": self.is_admin(),
                "version": VERSION,
            },
            extra={
                "Set-Cookie": f"{CSRF_COOKIE}={cookie}; HttpOnly; Secure; "
                f"SameSite=Strict; Path=/; Max-Age={CSRF_MAX_AGE}"
            },
        )

    def api_logout(self):
        # Deletes the session row, so the id is useless even if it was copied.
        self.server.sessions.delete(self.jar(SESSION_COOKIE))
        return self.send(200, {"ok": True}, extra={"Set-Cookie": Sessions.clear()})

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

    # Schedule changes go through the engine: activity log and Telegram alert with who did it.
    def api_add_schedule(self):
        s = validate_schedule(self.body())
        sid = self.engine.add_schedule(s, user=self.user)
        self.tick_soon()
        return self.send(201, {"id": sid})

    def api_edit_schedule(self, sid):
        sid = int(sid)
        if not self.engine.db.get_schedule(sid):
            raise LookupError("No such schedule")
        self.engine.update_schedule(sid, validate_schedule(self.body()), user=self.user)
        self.tick_soon()
        return self.send(200, {"ok": True})

    def api_delete_schedule(self, sid):
        self.engine.delete_schedule(int(sid), user=self.user)
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
                "scheduler_error": (
                    "The scheduler hit an error. See the server log."
                    if e.last_error
                    else None
                ),
                "scheduler_ok": bool(e.healthy()),
                "calendar_enabled": bool(cal and cal.url),
                "calendar_state": cal.state() if cal else "off",
                "calendar_last_ok": cal.last_ok if cal else None,
                "calendar_error": cal.last_error if cal else None,
                "alerts": e.notifier.stats(),
            },
        )

    # ---- People (admins only) ----
    def api_people(self):
        self.require_admin()
        g = self.server.google
        return self.send(
            200,
            {
                "admins": list(g.admins),
                "people": [
                    {
                        "email": p["email"].lower(),
                        "added_by": p["added_by"] or "",
                        "added_at": p["added_at"],
                    }
                    for p in self.engine.db.people()
                ],
            },
        )

    def api_add_person(self):
        self.require_admin()
        email = people.clean_email(self.body(4000).get("email"))
        if self.server.google.is_admin(email):
            raise ValueError(
                "%s is an admin (set in .env) and can already sign in." % email
            )
        db = self.engine.db
        if len(db.people()) >= MAX_PEOPLE:
            raise ValueError("The People list is full (%d)." % MAX_PEOPLE)
        if not db.person_add(email, self.user):
            raise ValueError("%s is already on the People list." % email)
        db.log(
            "",
            "people add",
            "web",
            True,
            "added %s to People" % email,
            user=self.user,
            kind="people",
        )
        self.engine.notifier.alert(
            "people",
            "People: %s added %s. They can now sign in to Door Control."
            % (self.user, email),
        )
        return self.send(201, {"ok": True, "email": email})

    def api_remove_person(self):
        self.require_admin()
        b = self.body(4000)
        email = str(b.get("email") or "").strip().lower()
        if not email:
            raise ValueError("Choose who to remove.")
        if self.server.google.is_admin(email):
            raise ValueError(
                "%s is an admin. Admins are set in .env on the server and can't be "
                "removed here." % email
            )
        if str(b.get("confirm") or "").strip().lower() != email:
            raise ValueError("Type the email address again to confirm the removal.")
        db = self.engine.db
        # Removes the person and ends their sessions in one transaction.
        if not db.person_remove(email):
            raise LookupError("%s is not on the People list." % email)
        db.log(
            "",
            "people remove",
            "web",
            True,
            "removed %s from People (signed out)" % email,
            user=self.user,
            kind="people",
        )
        self.engine.notifier.alert(
            "people",
            "People: %s removed %s. They were signed out and can no longer sign in."
            % (self.user, email),
        )
        return self.send(200, {"ok": True})

    def tick_soon(self):
        # The engine's tick lock: never two ticks at once; a busy tick runs once more.
        threading.Thread(target=self.engine.request_tick, daemon=True).start()
