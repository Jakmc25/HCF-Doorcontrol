"""Sign-in gate, Google sign-in, break-glass password, CSRF, host lockdown and the
who-did-it log. Google's token endpoint is faked; everything else is the real flow
(real web server, real engine, fake UniFi controller).

Run from the app folder: python3 tests/test_google.py
"""

import ast
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mock_unifi
from doorcontrol import app, auth, google_signin, web
from doorcontrol.db import DB
from doorcontrol.engine import Engine, validate_schedule
from doorcontrol.telegram_bot import Bot
from doorcontrol.unifi import UnifiClient

TZ = ZoneInfo("America/New_York")
HOST = "app.test"
LAN = "lan.test"
CLIENT_ID = "cid.apps.googleusercontent.com"
ENV = {
    "PUBLIC_HOSTS": HOST,
    "ALLOWED_HOSTS": LAN,
    "GOOGLE_CLIENT_ID": CLIENT_ID,
    "GOOGLE_CLIENT_SECRET": "shh",
    # As on fair-server from 1.2.0 (AC41): admins only, no domain-wide access.
    "GOOGLE_ALLOWED_DOMAINS": "",
    "GOOGLE_ALLOWED_EMAILS": "jake@hardincountyfair.org,kolt@hardincountyfair.org",
    # The test client connects from loopback and plays cloudflared (the trusted proxy).
    "TRUSTED_PROXIES": "127.0.0.1/32",
}
JAKE = "jake@hardincountyfair.org"
KOLT = "kolt@hardincountyfair.org"
ANN = "ann@hardincountyfair.org"
SID = web.SESSION_COOKIE
PASSWORD = "correct horse battery staple"

# Every route that needs a session, as (method, pattern) in web.py and an example path.
ROUTES = [
    ("GET", r"/api/bootstrap", "/api/bootstrap"),
    ("POST", r"/api/logout", "/api/logout"),
    ("GET", r"/api/doors", "/api/doors"),
    ("POST", r"/api/doors/([^/]+)/action", "/api/doors/d1/action"),
    ("GET", r"/api/schedules", "/api/schedules"),
    ("POST", r"/api/schedules", "/api/schedules"),
    ("PUT", r"/api/schedules/(\d+)", "/api/schedules/1"),
    ("DELETE", r"/api/schedules/(\d+)", "/api/schedules/1"),
    ("GET", r"/api/upcoming", "/api/upcoming"),
    ("GET", r"/api/log", "/api/log"),
    ("GET", r"/api/health", "/api/health"),
    ("GET", r"/api/people", "/api/people"),
    ("POST", r"/api/people", "/api/people"),
    ("POST", r"/api/people/remove", "/api/people/remove"),
]
CHANGE_ROUTES = [
    ("POST", "/api/doors/d1/action", {"action": "unlock_for", "minutes": 30}),
    (
        "POST",
        "/api/schedules",
        {
            "name": "New",
            "doors": ["d1"],
            "days": [0],
            "start": "08:00",
            "end": "09:00",
        },
    ),
    (
        "PUT",
        "/api/schedules/1",
        {
            "name": "Edited",
            "doors": ["d1"],
            "days": [0],
            "start": "08:00",
            "end": "09:00",
        },
    ),
    ("DELETE", "/api/schedules/1", None),
    ("POST", "/api/logout", None),
    ("POST", "/api/people", {"email": ANN}),
    ("POST", "/api/people/remove", {"email": ANN, "confirm": ANN}),
]


# ---- fake Google token endpoint (as Awards tests/test_google.py) ----
CLAIMS = {}
_real_urlopen = urllib.request.urlopen


class FakeResp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self.body


def _part(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def fake_urlopen(req, *args, **kwargs):
    if getattr(req, "full_url", req) != google_signin.TOKEN:
        return _real_urlopen(req, *args, **kwargs)  # UniFi mock calls pass through
    expected = "redirect_uri=" + urllib.parse.quote(
        f"https://{HOST}/api/auth/google/callback", safe=""
    )
    assert expected.encode() in req.data, req.data
    return FakeResp(json.dumps({"id_token": "x." + _part(CLAIMS) + ".sig"}).encode())


google_signin.urllib.request.urlopen = fake_urlopen


class Clock(Engine):
    fake_now = None

    def now(self):
        return self.fake_now or super().now()


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def header(self, name):
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def cookies(self):
        return [v for k, v in self.headers if k.lower() == "set-cookie"]

    def cookie(self, name):
        for c in self.cookies():
            if c.startswith(name + "="):
                return c
        return None

    def json(self):
        return json.loads(self.body)


class Stack:
    """Web server + engine + fake UniFi controller, with its own data folder."""

    def __init__(self, env=None, data_dir=None, max_connections=64):
        self.mock = mock_unifi.Mock()
        self.usrv = mock_unifi.make_server(self.mock)
        client = UnifiClient(
            "http://127.0.0.1:%d" % self.usrv.server_address[1], mock_unifi.TOKEN
        )
        self.data = data_dir or tempfile.mkdtemp()
        db = DB(os.path.join(self.data, "doorcontrol.db"))
        self.eng = Clock({"timezone": "America/New_York"}, db, client)
        with mock.patch.dict(os.environ, {**ENV, **(env or {})}):
            self.web = web.make_server(
                self.eng, self.data, "127.0.0.1", 0, max_connections
            )
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def close(self):
        self.web.shutdown()
        self.web.server_close()
        self.usrv.shutdown()
        self.usrv.server_close()

    def call(self, method, path, host=HOST, body=None, cookie="", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.web.server_address[1])
        h = {"Host": host}
        if body is not None:
            h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = cookie
        h.update(headers or {})
        c.request(
            method, path, body=json.dumps(body) if body is not None else None, headers=h
        )
        r = c.getresponse()
        out = Resp(r.status, r.getheaders(), r.read())
        c.close()
        return out

    # ---- sessions ----
    def session(self, email=JAKE, kind="google", gen=None):
        """A server-side session made directly (as a sign-in would), as a Cookie value."""
        return SID + "=" + self.web.sessions.create(email, kind, gen)

    def with_csrf(self, cookie):
        r = self.call("GET", "/api/bootstrap", cookie=cookie)
        assert r.status == 200, r.body
        return cookie + "; " + r.cookie("doors_session").split(";")[0], r.json()["csrf"]

    def change(self, method, path, body=None, email=JAKE, host=HOST):
        cookie, token = self.with_csrf(self.session(email))
        return self.call(
            method,
            path,
            host,
            body,
            cookie,
            {"X-CSRF-Token": token, "Origin": "https://" + host},
        )

    def google(self, email, verified=True, **over):
        """Full Google sign-in: start, then the callback with a faked token answer."""
        r = self.call("GET", "/api/auth/google/start")
        assert r.status == 302, r.status
        state = r.cookie("google_state").split(";")[0].split("=", 1)[1]
        CLAIMS.clear()
        CLAIMS.update(
            {
                "iss": "https://accounts.google.com",
                "aud": CLIENT_ID,
                "exp": time.time() + 300,
                "nonce": self.web.google.mac("nonce." + state),
                "email": email,
                "email_verified": verified,
            }
        )
        if email.lower().endswith("@hardincountyfair.org"):
            CLAIMS["hd"] = "hardincountyfair.org"
        CLAIMS.update(over)
        return self.callback(state, state)

    def callback(self, state, cookie_state, **extra):
        q = urllib.parse.urlencode({"state": state, "code": "abc", **extra})
        return self.call(
            "GET",
            google_signin.CALLBACK + "?" + q,
            cookie=f"google_state={cookie_state}",
        )

    def log_rows(self):
        return self.eng.db.recent_log(2000)

    def add_schedule(self, **kw):
        base = {
            "name": "Office hours",
            "doors": ["d2"],
            "days": [0, 1, 2, 3, 4],
            "start": "08:00",
            "end": "17:00",
        }
        base.update(kw)
        return self.eng.db.add_schedule(validate_schedule(base))


def attrs(set_cookie):
    """'a=b; HttpOnly; Max-Age=5' -> {'httponly': True, 'max-age': '5'}"""
    out = {}
    for part in set_cookie.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        out[k.lower()] = v if _ else True
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.st = Stack()
        self.addCleanup(self.st.close)


# ---------------------------------------------------------------- gate
class GateTests(Base):
    def test_route_list_matches_web(self):
        self.assertEqual(
            [(m, p) for m, p, _ in web.ROUTES], [(m, p) for m, p, _ in ROUTES]
        )
        for m, pattern, example in ROUTES:
            self.assertTrue(re.fullmatch(pattern, example))
        for _, _, name in web.ROUTES:
            self.assertTrue(hasattr(web.Handler, "api_" + name), name)
        # Routing only goes through the table: no other "/api/" path in web.py's handler code.
        src = read("doorcontrol", "web.py")
        literal = set(re.findall(r'"(/api/[a-z/_\-]*)', src))
        allowed = {p for _, p in web.SIGNIN_ROUTES} | {"/api/auth/google/start"}
        allowed |= {p for _, p, _ in ROUTES if "(" not in p}
        allowed |= {"/api/doors/", "/api/schedules/"}
        self.assertLessEqual(literal, allowed)

    def test_every_api_route_needs_sign_in(self):
        sid = self.st.add_schedule()
        self.assertEqual(sid, 1)
        before_sched = self.st.eng.db.schedules()
        before_log = self.st.log_rows()
        calls = len(self.st.mock.calls)
        paths = sorted({p for _, _, p in ROUTES} | {"/api/nope", "/api/accounts"})
        for host in (HOST, LAN):
            for path in paths:
                for method in ("GET", "POST", "PUT", "DELETE"):
                    with self.subTest(host=host, method=method, path=path):
                        r = self.st.call(method, path, host, body={"action": "lock"})
                        self.assertEqual(r.status, 401)
                        self.assertLessEqual(set(r.json()), {"error", "signin"})
                        self.assertNotIn(b"Event Center", r.body)
                        self.assertIsNone(r.cookie(SID))
        self.assertEqual(len(self.st.mock.calls), calls, "no UniFi call")
        self.assertEqual(self.st.eng.db.schedules(), before_sched)
        self.assertEqual(self.st.log_rows(), before_log)

    def test_signin_page_and_static_allowlist(self):
        r = self.st.call("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b'id="login"', r.body)
        self.assertNotIn(b'id="doors"', r.body)
        self.assertNotIn(b"Event Center", r.body)
        for name in ("login.js", "style.css", "fair-logo.png"):
            with self.subTest(name=name):
                self.assertEqual(self.st.call("GET", "/static/" + name).status, 200)
        for name in ("app.js", "index.html", "login.html", "nope.css", "../web.py"):
            with self.subTest(name=name):
                self.assertIn(self.st.call("GET", "/static/" + name).status, (401, 404))
        # Signed in: path tricks still stay inside static/.
        ck = self.st.session()
        self.assertEqual(self.st.call("GET", "/static/app.js", cookie=ck).status, 200)
        for bad in (
            "/static/../web.py",
            "/static/%2e%2e/web.py",
            "/static/x/../../app.py",
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self.st.call("GET", bad, cookie=ck).status, 404)

    def test_error_and_login_pages_static_files_are_allowed(self):
        """I6: everything the sign-in and sign-in-problem pages load works without a session."""
        login = read("doorcontrol", "static", "login.html")
        pages = [google_signin.page("x"), login]
        refs = set()
        for p in pages:
            refs |= set(re.findall(r'(?:href|src)="/static/([^"]+)"', p))
        self.assertEqual(refs, web.LOGIN_STATIC)
        for name in refs:
            self.assertEqual(self.st.call("GET", "/static/" + name).status, 200)
        css = read("doorcontrol", "static", "style.css")
        used = set()
        for p in pages:
            for cls in re.findall(r'class="([^"]+)"', p):
                used |= set(cls.split())
        for cls in used:
            with self.subTest(cls=cls):
                self.assertRegex(css, r"\." + re.escape(cls) + r"\b")

    def test_bad_session_cookies_rejected(self):
        st = self.st
        good = st.session()
        sid = good.split("=", 1)[1]
        stored = st.eng.db.conn.execute("SELECT id_hash FROM sessions").fetchone()[0]
        # 1.1.0's stateless cookie, still correctly signed with this server's secret.
        g = google_signin.Google(st.web.secret, "doors_auth")
        old = "doors_auth=" + g.issue(JAKE)
        bad = {
            "random id": SID + "=" + "A" * 43,
            "tampered id": SID + "=" + sid[:-1] + ("A" if sid[-1] != "A" else "B"),
            "stored hash as id": SID + "=" + stored,
            "empty": SID + "=",
            "too long": SID + "=" + "a" * 500,
            "old stateless cookie": old,
            "old cookie name with new id": "doors_auth=" + sid,
            "garbage": SID + "=x",
        }
        self.assertEqual(st.call("GET", "/api/doors", cookie=good).status, 200)
        for label, value in bad.items():
            with self.subTest(label):
                r = st.call("GET", "/api/doors", cookie=value)
                self.assertEqual(r.status, 401)

    def test_changes_need_csrf_and_same_origin(self):
        self.st.add_schedule()
        before_sched = self.st.eng.db.schedules()
        before_log = self.st.log_rows()
        puts = lambda: [c for c in self.st.mock.calls if c[0] == "PUT"]
        cookie, token = self.st.with_csrf(self.st.session())
        auth_only = self.st.session()
        cases = {
            "no token": (cookie, {}),
            "wrong token": (cookie, {"X-CSRF-Token": "0" * 64}),
            "token without its cookie": (auth_only, {"X-CSRF-Token": token}),
            "other origin": (
                cookie,
                {"X-CSRF-Token": token, "Origin": "https://evil.test"},
            ),
            "lookalike origin": (
                cookie,
                {"X-CSRF-Token": token, "Origin": "https://app.test.evil.test"},
            ),
        }
        for method, path, body in CHANGE_ROUTES:
            for label, (ck, headers) in cases.items():
                with self.subTest(route=method + " " + path, case=label):
                    r = self.st.call(method, path, HOST, body, ck, headers)
                    self.assertEqual(r.status, 403)
                    self.assertIsNone(r.cookie(SID))
        time.sleep(0.2)
        self.assertEqual(puts(), [], "no door changed")
        self.assertEqual(self.st.eng.db.schedules(), before_sched)
        self.assertEqual(self.st.log_rows(), before_log)
        # The right token and origin work.
        r = self.st.call(
            "POST",
            "/api/doors/d1/action",
            HOST,
            {"action": "lock"},
            cookie,
            {"X-CSRF-Token": token, "Origin": "https://app.test"},
        )
        self.assertEqual(r.status, 200, r.body)

    def test_basic_auth_not_accepted(self):
        basic = {"Authorization": "Basic " + base64.b64encode(b"x:change-me").decode()}
        for method, _, path in ROUTES:
            with self.subTest(path=path):
                r = self.st.call(method, path, headers=basic)
                self.assertEqual(r.status, 401)
                self.assertIsNone(r.header("WWW-Authenticate"))

    def test_server_error_hides_exception_text(self):
        def boom():
            raise RuntimeError("secret-detail-xyz")

        self.st.eng.status = boom
        with self.assertLogs("doorcontrol", "ERROR") as logs:
            r = self.st.call("GET", "/api/doors", cookie=self.st.session())
        self.assertEqual(r.status, 500)
        self.assertEqual(r.json(), {"error": web.SERVER_ERROR})
        self.assertNotIn(b"secret-detail-xyz", r.body)
        self.assertIn("secret-detail-xyz", "\n".join(logs.output))


# ---------------------------------------------------------------- hosts
class HostTests(Base):
    def test_other_cloudflare_host_404(self):
        cf = {"CF-Connecting-IP": "203.0.113.9"}
        ck = self.st.session()
        for method, path in [
            ("GET", "/"),
            ("GET", "/api/doors"),
            ("GET", "/api/login-info"),
            ("GET", "/static/style.css"),
            ("GET", "/api/auth/google/start"),
            ("POST", "/api/login"),
        ]:
            with self.subTest(path=path):
                r = self.st.call(method, path, "other.jakemc.me", cookie=ck, headers=cf)
                self.assertEqual(r.status, 404)
                self.assertIsNone(r.header("Location"))
        # Unknown host that did not come through Cloudflare: refused too.
        self.assertEqual(self.st.call("GET", "/", "evil.test").status, 403)

    def test_google_start_not_on_lan(self):
        for path in ("/api/auth/google/start", google_signin.CALLBACK + "?state=1.a"):
            r = self.st.call("GET", path, LAN)
            self.assertEqual(r.status, 404)
            self.assertIsNone(r.header("Location"))
            self.assertIsNone(r.cookie("google_state"))

    def test_lan_needs_signin_and_points_to_public_host(self):
        info = self.st.call("GET", "/api/login-info", LAN).json()
        self.assertTrue(info["google"])
        self.assertEqual(info["google_url"], f"https://{HOST}/api/auth/google/start")
        self.assertFalse(info["remote"])
        self.assertEqual(self.st.call("GET", "/api/doors", LAN).status, 401)
        self.assertIn(b'id="login"', self.st.call("GET", "/", LAN).body)
        # The admin password only works at the public hostname.
        self.st.web.auth.set_password("admin", PASSWORD)
        r = self.st.call(
            "POST", "/api/login", LAN, {"name": "admin", "password": PASSWORD}
        )
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.cookie(SID))

    def test_empty_allowed_hosts_refuses(self):
        st = Stack({"ALLOWED_HOSTS": ""})
        self.addCleanup(st.close)
        for host in (LAN, "localhost:8480", "127.0.0.1:8480"):
            self.assertEqual(st.call("GET", "/", host).status, 403)
        self.assertEqual(st.call("GET", "/", HOST).status, 200)


# ---------------------------------------------------------------- Google
class GoogleTests(Base):
    def test_workspace_account_signs_in(self):
        r = self.st.google("Jake@HardinCountyFair.org")
        self.assertEqual(r.status, 200)
        self.assertIn(b'http-equiv="refresh"', r.body)
        ck = r.cookie(SID).split(";")[0]
        self.assertRegex(ck, "^" + re.escape(SID) + r"=[A-Za-z0-9_-]{43}$")
        self.assertEqual(
            self.st.call("GET", "/api/bootstrap", cookie=ck).json()["user"], JAKE
        )
        page = self.st.call("GET", "/", cookie=ck)
        self.assertIn(b'id="doors"', page.body)

    def test_refusals(self):
        st = self.st
        r = st.call("GET", "/api/auth/google/start")
        old_state = r.cookie("google_state").split(";")[0].split("=", 1)[1]
        stale = "%d.%s" % (time.time() - 601, "a" * 32)
        cases = {
            "other domain": lambda: st.google("someone@gmail.com"),
            "workspace-looking other domain": lambda: st.google(
                "x@hardincountyfair.org.evil.com"
            ),
            "email not verified": lambda: st.google(
                "x@hardincountyfair.org", verified=False
            ),
            "hd missing": lambda: st.google("x@hardincountyfair.org", hd=None),
            "wrong aud": lambda: st.google("x@hardincountyfair.org", aud="other-app"),
            "replayed nonce": lambda: st.google(
                "x@hardincountyfair.org", nonce=st.web.google.mac("nonce." + old_state)
            ),
            "state not matching cookie": lambda: st.callback(old_state, "1.other"),
            "state older than 10 minutes": lambda: st.callback(stale, stale),
            "google error param": lambda: st.callback(
                old_state, old_state, error="access_denied"
            ),
        }
        before = len(st.log_rows())
        for label, attempt in cases.items():
            with self.subTest(label):
                with self.assertLogs("doorcontrol", "WARNING") as logs:
                    r = attempt()
                self.assertEqual(r.status, 403)
                self.assertIsNone(r.cookie(SID))
                self.assertIn(b"Sign-in problem", r.body)
                self.assertNotIn(b"Traceback", r.body)
                self.assertTrue(
                    any("google sign-in refused" in line for line in logs.output)
                )
        # 1.2.0 (AC41): every refusal is also an activity-log row (sign-in kind, failed).
        rows = st.log_rows()[: len(cases)]
        self.assertEqual(len(st.log_rows()), before + len(cases))
        for row in rows:
            self.assertEqual(
                (row["action"], row["ok"], row["kind"], row["user"]),
                ("sign-in refused", 0, "signin", ""),
            )
        self.assertEqual(st.eng.db.sessions_list(), [], "no session for a refusal")

    def test_outside_account_refused(self):
        self.assertEqual(self.st.web.google.emails, [JAKE, KOLT])
        with self.assertLogs("doorcontrol", "WARNING"):
            r = self.st.google("volunteer@gmail.com")
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.cookie(SID))

    def test_removed_from_env_refused_after_restart(self):
        ck = self.st.google(KOLT).cookie(SID).split(";")[0]
        self.assertEqual(self.st.call("GET", "/api/doors", cookie=ck).status, 200)
        # Restart with the same settings: the session survives (it is in the database).
        same = Stack(data_dir=self.st.data)
        self.addCleanup(same.close)
        self.assertEqual(same.call("GET", "/api/doors", cookie=ck).status, 200)
        # Restart with Kolt removed from GOOGLE_ALLOWED_EMAILS: refused at once.
        changed = Stack({"GOOGLE_ALLOWED_EMAILS": JAKE}, data_dir=self.st.data)
        self.addCleanup(changed.close)
        r = changed.call("GET", "/api/doors", cookie=ck)
        self.assertEqual(r.status, 401)
        self.assertEqual(attrs(r.cookie(SID))["max-age"], "0", "cookie cleared")
        self.assertEqual(changed.eng.db.sessions_list(), [], "row deleted")

    def test_not_configured_404_and_app_runs(self):
        st = Stack({"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""})
        self.addCleanup(st.close)
        r = st.call("GET", "/api/auth/google/start")
        self.assertEqual(r.status, 404)
        self.assertIsNone(r.header("Location"))
        self.assertIn("set up".encode(), r.body)
        self.assertFalse(st.call("GET", "/api/login-info").json()["google"])
        # Scheduler and Telegram still work.
        st.add_schedule()
        st.eng.fake_now = datetime(2027, 9, 6, 9, 0, tzinfo=TZ)
        st.eng.tick()
        self.assertEqual(st.mock.doors["d2"]["door_lock_relay_status"], "unlock")
        bot = Bot({"allowed_chat_ids": [42]}, st.eng)
        self.assertIn("locked now", bot.handle(42, "/lock office"))
        # And the real app starts with no Google settings.
        data = tempfile.mkdtemp()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = {
            "timezone": "America/New_York",
            "unifi": {
                "host": "http://127.0.0.1:%d" % st.usrv.server_address[1],
                "token": mock_unifi.TOKEN,
            },
            "tick_seconds": 3600,
        }
        with open(os.path.join(data, "config.json"), "w") as f:
            json.dump(cfg, f)
        env = {
            **ENV,
            "GOOGLE_CLIENT_ID": "",
            "GOOGLE_CLIENT_SECRET": "",
            "DATA_DIR": data,
            "BIND": "127.0.0.1",
            "PORT": str(port),
            "ALLOWED_HOSTS": "127.0.0.1:%d" % port,
            # 1.1.0 (AC42): plain http only to a local fake console, by explicit choice.
            "DOORCONTROL_DEV_ALLOW_HTTP": "1",
        }
        with mock.patch.dict(os.environ, env):
            threading.Thread(target=app.main, args=([],), daemon=True).start()
            for _ in range(100):
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    c.request("GET", "/health")
                    up = c.getresponse().status == 200
                    c.close()
                    if up:
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("app did not start")
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/api/login-info")
        self.assertFalse(json.loads(c.getresponse().read())["google"])
        c.close()
        self.assertTrue(os.path.exists(os.path.join(data, "doorcontrol.db")))
        self.assertTrue(os.path.exists(os.path.join(data, "session-secret")))


# ---------------------------------------------------------------- sessions
class SessionTests(Base):
    def test_cookie_flags(self):
        r = self.st.call("GET", "/api/auth/google/start")
        a = attrs(r.cookie("google_state"))
        self.assertTrue(a["httponly"] and a["secure"])
        self.assertEqual(a["samesite"], "Lax")
        self.assertEqual(a["path"], "/api/auth/google/")
        self.assertEqual(a["max-age"], "600")
        a = attrs(self.st.google(JAKE).cookie(SID))
        self.assertTrue(SID.startswith("__Host-"))
        self.assertTrue(a["httponly"] and a["secure"])
        self.assertEqual(a["samesite"], "Strict")
        self.assertEqual(a["path"], "/")
        self.assertNotIn("domain", a)
        self.assertEqual(a["max-age"], str(12 * 3600))
        r = self.st.call("GET", "/api/bootstrap", cookie=self.st.session())
        a = attrs(r.cookie("doors_session"))
        self.assertTrue(a["httponly"] and a["secure"])
        self.assertEqual(a["samesite"], "Strict")

    def test_sign_out_needs_csrf_and_clears_cookie(self):
        r = self.st.call("POST", "/api/logout", cookie=self.st.session())
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.cookie(SID))
        r = self.st.change("POST", "/api/logout")
        self.assertEqual(r.status, 200)
        c = r.cookie(SID)
        self.assertTrue(c.startswith(SID + "=;"))
        a = attrs(c)
        self.assertEqual(a["max-age"], "0")
        self.assertTrue(a["secure"] and a["httponly"])

    def test_lifetime_constant_and_expiry(self):
        self.assertEqual(web.SESSION_HOURS, 12)
        self.assertFalse(hasattr(web, "SESSION_DAYS"))
        sessions = self.st.web.sessions
        t0 = 2_000_000_000
        sessions.clock = lambda: t0
        ck = self.st.session()
        sessions.clock = lambda: t0 + 12 * 3600 - 1
        self.assertEqual(self.st.call("GET", "/api/doors", cookie=ck).status, 200)
        sessions.clock = lambda: t0 + 12 * 3600
        self.assertEqual(self.st.call("GET", "/api/doors", cookie=ck).status, 401)
        self.assertEqual(self.st.eng.db.sessions_list(), [], "expired row deleted")


# ---------------------------------------------------------------- 1.2.0 sessions (AC40)
class ServerSessionTests(Base):
    def test_db_stores_hash_only(self):
        r = self.st.google(JAKE)
        sid = r.cookie(SID).split(";")[0].split("=", 1)[1]
        rows = [dict(x) for x in self.st.eng.db.conn.execute("SELECT * FROM sessions")]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id_hash"], hashlib.sha256(sid.encode()).hexdigest())
        self.assertEqual((row["user"], row["kind"]), (JAKE, "google"))
        self.assertAlmostEqual(row["expires"] - row["created"], 12 * 3600)
        with open(os.path.join(self.st.data, "doorcontrol.db"), "rb") as f:
            self.assertNotIn(sid.encode(), f.read(), "the id itself is never stored")

    def test_expires_after_12h(self):
        st = self.st
        st.web.sessions.clock = lambda: 1000.0
        ck = st.session()
        st.web.sessions.clock = lambda: 1000.0 + 12 * 3600 + 1
        self.assertEqual(st.call("GET", "/api/doors", cookie=ck).status, 401)

    def test_expired_rows_purged_hourly(self):
        st = self.st
        st.web.sessions.clock = lambda: 1000.0
        st.session()
        st.session(KOLT)
        st.web.sessions.clock = lambda: 1000.0 + 13 * 3600
        st.web.sessions._purged = 0.0
        st.call("GET", "/api/doors")  # any request runs the hourly purge
        self.assertEqual(st.eng.db.sessions_list(), [])

    def test_manage_sessions_and_revoke(self):
        st = self.st
        env = {**os.environ, "DATA_DIR": st.data}

        def cli(*args):
            return subprocess.run(
                [sys.executable, os.path.join(ROOT, "manage.py"), *args],
                capture_output=True,
                text=True,
                env=env,
                cwd=ROOT,
            )

        jake, kolt = st.session(JAKE), st.session(KOLT)
        out = cli("sessions")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(JAKE, out.stdout)
        self.assertIn(KOLT, out.stdout)
        self.assertNotIn(jake.split("=", 1)[1], out.stdout)
        r = cli("revoke", "Jake@HardinCountyFair.org")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ended 1 session", r.stdout)
        self.assertEqual(st.call("GET", "/api/doors", cookie=jake).status, 401)
        self.assertEqual(st.call("GET", "/api/doors", cookie=kolt).status, 200)
        st.web.auth.set_password("admin", PASSWORD)
        gen = st.web.auth.load()["admin"]["gen"]
        admin = st.session("admin:admin", "admin", gen)
        self.assertEqual(st.call("GET", "/api/doors", cookie=admin).status, 200)
        self.assertIn("Ended 1", cli("revoke", "admin:admin").stdout)
        self.assertEqual(st.call("GET", "/api/doors", cookie=admin).status, 401)
        st.session(JAKE)
        self.assertIn("Ended 2", cli("revoke", "--all").stdout)
        self.assertEqual(st.call("GET", "/api/doors", cookie=kolt).status, 401)
        self.assertIn("Nobody is signed in", cli("sessions").stdout)
        self.assertNotEqual(cli("revoke").returncode, 0)

    def test_set_password_revokes_admin_session(self):
        st = self.st
        st.web.auth.set_password("admin", PASSWORD)
        r = st.call(
            "POST",
            "/api/login",
            body={"name": "admin", "password": PASSWORD},
            headers={"CF-Connecting-IP": "198.51.100.60"},
        )
        ck = r.cookie(SID).split(";")[0]
        self.assertEqual(st.call("GET", "/api/doors", cookie=ck).status, 200)
        st.web.auth.set_password("admin", PASSWORD + "!")  # new gen
        self.assertEqual(st.call("GET", "/api/doors", cookie=ck).status, 401)
        # remove-user too (through the CLI, which also deletes the rows).
        st.web.auth.set_password("admin", PASSWORD)
        r = st.call(
            "POST",
            "/api/login",
            body={"name": "admin", "password": PASSWORD},
            headers={"CF-Connecting-IP": "198.51.100.61"},
        )
        ck = r.cookie(SID).split(";")[0]
        env = {**os.environ, "DATA_DIR": st.data}
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "manage.py"), "remove-user", "admin"],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(st.call("GET", "/api/doors", cookie=ck).status, 401)
        self.assertEqual(st.eng.db.sessions_list(), [])

    def test_logout_deletes_row(self):
        st = self.st
        ck, token = st.with_csrf(st.session())
        self.assertEqual(len(st.eng.db.sessions_list()), 1)
        r = st.call(
            "POST",
            "/api/logout",
            body=None,
            cookie=ck,
            headers={"X-CSRF-Token": token, "Origin": "https://" + HOST},
        )
        self.assertEqual(r.status, 200)
        self.assertEqual(st.eng.db.sessions_list(), [])
        self.assertEqual(st.call("GET", "/api/doors", cookie=ck).status, 401)

    def test_old_doors_auth_cookie_refused(self):
        st = self.st
        old = google_signin.Google(st.web.secret, "doors_auth").issue(JAKE)
        r = st.call("GET", "/api/doors", cookie="doors_auth=" + old)
        self.assertEqual(r.status, 401)
        c = r.cookie("doors_auth")
        self.assertTrue(c.startswith("doors_auth=;"), "the old cookie is cleared")
        self.assertEqual(attrs(c)["max-age"], "0")
        # A browser with both: the new session works and the old cookie is cleared.
        r = st.call(
            "GET", "/api/doors", cookie="doors_auth=%s; %s" % (old, st.session())
        )
        self.assertEqual(r.status, 200)
        self.assertTrue(r.cookie("doors_auth").startswith("doors_auth=;"))
        # Stateless issue/verify of the shared modules are not used by web.py any more.
        src = read("doorcontrol", "web.py")
        for gone in (".issue(", ".verify(", "cookie_header(", "DAYS"):
            self.assertNotIn(gone, src)


# ---------------------------------------------------------------- 1.2.0 who signs in (AC41)
class WhoSignsInTests(Base):
    def test_only_admins_and_people_sign_in_domain_account_refused_and_logged(self):
        st = self.st
        self.assertEqual(st.google(JAKE).status, 200)
        self.assertEqual(st.google(KOLT).status, 200)
        with self.assertLogs("doorcontrol", "WARNING") as logs:
            r = st.google(ANN)
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.cookie(SID))
        self.assertIn(ANN, "\n".join(logs.output))
        row = st.log_rows()[0]
        self.assertEqual(
            (row["action"], row["ok"], row["kind"]), ("sign-in refused", 0, "signin")
        )
        self.assertIn(ANN, row["detail"])
        # No hd hint and no domain-wide access with an empty domain list.
        start = st.call("GET", "/api/auth/google/start").header("Location")
        self.assertNotIn("hd=", start)
        # Added to People by an admin: now Ann can sign in.
        st.eng.db.person_add(ANN, JAKE)
        self.assertEqual(st.google(ANN).status, 200)
        self.assertEqual(st.google("Ann@HardinCountyFair.org").status, 200)

    def test_all_empty_no_google_breakglass_works(self):
        st = Stack({"GOOGLE_ALLOWED_EMAILS": "", "GOOGLE_ALLOWED_DOMAINS": ""})
        self.addCleanup(st.close)
        self.assertFalse(st.web.google.ready())
        self.assertFalse(st.call("GET", "/api/login-info").json()["google"])
        r = st.call("GET", "/api/auth/google/start")
        self.assertEqual(r.status, 404)
        self.assertIsNone(r.header("Location"))
        st.web.auth.set_password("admin", PASSWORD)
        r = st.call(
            "POST",
            "/api/login",
            body={"name": "admin", "password": PASSWORD},
            headers={"CF-Connecting-IP": "198.51.100.70"},
        )
        self.assertEqual(r.status, 200)
        ck = r.cookie(SID).split(";")[0]
        b = st.call("GET", "/api/bootstrap", cookie=ck).json()
        self.assertEqual((b["user"], b["admin"]), ("admin:admin", False))

    def test_door_google_relies_on_parent_behaviour(self):
        """The subclass relies on the pinned parent assigning self.emails in __init__ and
        reading self.emails in finish(), ready(), start() and allowed()."""
        src = read("doorcontrol", "google_signin.py")
        self.assertIn('self.emails = _list("GOOGLE_ALLOWED_EMAILS")', src)
        self.assertIn("email in self.emails", src)
        self.assertIn("(self.domains or self.emails)", src)
        db = self.st.eng.db
        with mock.patch.dict(
            os.environ,
            {
                "GOOGLE_ALLOWED_EMAILS": "Jake@HardinCountyFair.org",
                "GOOGLE_ALLOWED_DOMAINS": "",
            },
        ):
            g = web.DoorGoogle(b"s", SID, db)
        self.assertEqual(g.admins, [JAKE])
        self.assertEqual(g.emails, [JAKE])
        self.assertFalse(g.allowed(ANN))
        db.person_add(ANN, JAKE)
        self.assertEqual(g.emails, [ANN, JAKE], "people list read live")
        self.assertTrue(g.allowed("ANN@hardincountyfair.org"))
        self.assertTrue(g.is_admin(JAKE))
        self.assertFalse(g.is_admin(ANN))
        with mock.patch.dict(
            os.environ, {"GOOGLE_ALLOWED_EMAILS": "", "GOOGLE_ALLOWED_DOMAINS": ""}
        ):
            g = web.DoorGoogle(b"s", SID, db)
        g.client_id, g.client_secret = "id", "secret"
        self.assertTrue(g.ready(), "a listed person makes Google usable")
        db.person_remove(ANN)
        self.assertFalse(g.ready())


# ---------------------------------------------------------------- 1.2.0 People (AC41a)
class PeopleTests(Base):
    def admin(self, method, path, body=None, email=JAKE):
        return self.st.change(method, path, body, email=email)

    def test_admin_add_validates_format_case_duplicates(self):
        st = self.st
        r = self.admin("POST", "/api/people", {"email": "  Ann@HardinCountyFair.ORG "})
        self.assertEqual(r.status, 201, r.body)
        self.assertEqual(r.json()["email"], ANN)
        for bad, word in (
            ("", "Type"),
            ("ann", "email"),
            ("ann@", "email"),
            ("@hardincountyfair.org", "email"),
            ("a b@hardincountyfair.org", "email"),
            ("a@b@hardincountyfair.org", "email"),
            ("x,y@hardincountyfair.org", "email"),
            ("x@localhost", "email"),
            ("x" * 250 + "@a.org", "email"),
            (ANN.upper(), "already on the People list"),
            (JAKE, "admin"),
            ("KOLT@hardincountyfair.org", "admin"),
        ):
            with self.subTest(bad):
                r = self.admin("POST", "/api/people", {"email": bad})
                self.assertEqual(r.status, 400)
                self.assertIn(word, r.json()["error"])
        listed = self.admin("GET", "/api/people").json()
        self.assertEqual(listed["admins"], [JAKE, KOLT])
        self.assertEqual([p["email"] for p in listed["people"]], [ANN])
        self.assertEqual(listed["people"][0]["added_by"], JAKE)

    def test_remove_needs_typed_confirm(self):
        st = self.st
        st.eng.db.person_add(ANN, JAKE)
        for confirm in (None, "", "ann", "bob@hardincountyfair.org"):
            with self.subTest(confirm=confirm):
                r = self.admin(
                    "POST", "/api/people/remove", {"email": ANN, "confirm": confirm}
                )
                self.assertEqual(r.status, 400)
                self.assertIn("confirm", r.json()["error"])
        self.assertEqual(st.eng.db.people_emails(), {ANN})
        r = self.admin(
            "POST",
            "/api/people/remove",
            {"email": ANN, "confirm": " Ann@HardinCountyFair.org"},
        )
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(st.eng.db.people_emails(), set())
        r = self.admin("POST", "/api/people/remove", {"email": ANN, "confirm": ANN})
        self.assertEqual(r.status, 404)

    def test_non_admin_403(self):
        st = self.st
        st.eng.db.person_add(ANN, JAKE)
        st.web.auth.set_password("admin", PASSWORD)
        gen = st.web.auth.load()["admin"]["gen"]
        for who, cookie in (
            ("person", st.session(ANN)),
            ("break-glass", st.session("admin:admin", "admin", gen)),
        ):
            ck, token = st.with_csrf(cookie)
            b = st.call("GET", "/api/bootstrap", cookie=ck).json()
            self.assertFalse(b["admin"], who)
            h = {"X-CSRF-Token": token, "Origin": "https://" + HOST}
            for method, path, body in (
                ("GET", "/api/people", None),
                ("POST", "/api/people", {"email": "bob@hardincountyfair.org"}),
                ("POST", "/api/people/remove", {"email": ANN, "confirm": ANN}),
            ):
                with self.subTest(who=who, path=path, method=method):
                    r = st.call(method, path, HOST, body, ck, h)
                    self.assertEqual(r.status, 403)
                    self.assertNotIn(ANN.encode(), r.body)
        self.assertEqual(st.eng.db.people_emails(), {ANN})
        self.assertTrue(
            st.call("GET", "/api/bootstrap", cookie=st.session()).json()["admin"]
        )

    def test_admin_cannot_be_removed(self):
        for who in (JAKE, KOLT.upper()):
            r = self.admin("POST", "/api/people/remove", {"email": who, "confirm": who})
            self.assertEqual(r.status, 400)
            self.assertIn(".env", r.json()["error"])
        self.assertEqual(
            self.st.call("GET", "/api/doors", cookie=self.st.session(KOLT)).status, 200
        )

    def test_remove_ends_sessions_at_once(self):
        st = self.st
        st.eng.db.person_add(ANN, JAKE)
        ann = st.google(ANN).cookie(SID).split(";")[0]
        ann2 = st.session(ANN)
        jake = st.session(JAKE)
        self.assertEqual(st.call("GET", "/api/doors", cookie=ann).status, 200)
        r = self.admin("POST", "/api/people/remove", {"email": ANN, "confirm": ANN})
        self.assertEqual(r.status, 200)
        users = [s["user"] for s in st.eng.db.sessions_list()]
        self.assertNotIn(ANN, users, "rows deleted in the same transaction")
        for ck in (ann, ann2):
            self.assertEqual(st.call("GET", "/api/doors", cookie=ck).status, 401)
        self.assertEqual(st.call("GET", "/api/doors", cookie=jake).status, 200)
        with self.assertLogs("doorcontrol", "WARNING"):
            self.assertEqual(st.google(ANN).status, 403)

    def test_add_remove_logged_and_alerted(self):
        st = self.st
        sent = []
        st.eng.notifier.alert = lambda kind, text, key=None: sent.append(
            (kind, text, key)
        )
        self.admin("POST", "/api/people", {"email": ANN}, email=KOLT)
        self.admin("POST", "/api/people/remove", {"email": ANN, "confirm": ANN})
        rows = [r for r in st.log_rows() if r["kind"] == "people"]
        got = [(r["action"], r["user"], r["ok"]) for r in reversed(rows)]
        self.assertEqual(got, [("people add", KOLT, 1), ("people remove", JAKE, 1)])
        self.assertIn(ANN, rows[0]["detail"])
        self.assertEqual([k for k, _, _ in sent], ["people", "people"])
        self.assertIn(KOLT, sent[0][1])
        self.assertIn(ANN, sent[0][1])
        self.assertIn(JAKE, sent[1][1])
        self.assertIn("signed out", sent[1][1])
        # Refused changes write nothing and alert nothing.
        n = len(st.log_rows())
        self.admin("POST", "/api/people", {"email": "nope"})
        self.admin("POST", "/api/people/remove", {"email": ANN, "confirm": "x"})
        self.assertEqual(len(st.log_rows()), n)
        self.assertEqual(len(sent), 2)

    def test_people_are_calendar_organizers(self):
        from doorcontrol import people

        db = self.st.eng.db
        env = {"GOOGLE_ALLOWED_EMAILS": JAKE}
        self.assertEqual(people.allowed_organizers({}, env, db), {JAKE})
        db.person_add(ANN, JAKE)
        self.assertEqual(people.allowed_organizers({}, env, db), {JAKE, ANN})
        cfg = {"allowed_organizers": ["doors@group.calendar.test"]}
        self.assertEqual(
            people.allowed_organizers(cfg, env, db), {"doors@group.calendar.test"}
        )

    def test_migration_adds_tables_only(self):
        cols = lambda t: [
            r[1] for r in self.st.eng.db.conn.execute("PRAGMA table_info(%s)" % t)
        ]
        self.assertEqual(
            cols("sessions"),
            ["id_hash", "user", "kind", "gen", "created", "expires", "ip"],
        )
        self.assertEqual(cols("people"), ["email", "added_by", "added_at"])


# ---------------------------------------------------------------- break-glass
class BreakGlassTests(Base):
    def login(self, name, password, ip="198.51.100.1"):
        return self.st.call(
            "POST",
            "/api/login",
            body={"name": name, "password": password},
            headers={"CF-Connecting-IP": ip},
        )

    def test_rate_limit(self):
        self.st.web.auth.set_password("admin", PASSWORD)
        ip = "198.51.100.17"
        for i in range(10):
            self.assertEqual(self.login("admin", "wrong", ip).status, 401, i)
        self.assertEqual(self.login("admin", "wrong", ip).status, 429)
        r = self.login("admin", PASSWORD, ip)
        self.assertEqual(r.status, 429, "even the right password waits")
        self.assertIsNone(r.cookie(SID))
        self.assertEqual(self.login("admin", PASSWORD, "198.51.100.18").status, 200)

    def test_password_signin_and_log_row(self):
        self.st.web.auth.set_password("admin", PASSWORD)
        r = self.login("admin", "not it", "198.51.100.30")
        self.assertEqual(r.status, 401)
        self.assertEqual(r.json(), {"error": "Wrong name or password."})
        self.assertIsNone(r.cookie(SID))
        with self.assertLogs("doorcontrol", "INFO") as logs:
            r = self.login("Admin", PASSWORD, "198.51.100.31")
        self.assertEqual(r.status, 200)
        self.assertNotIn(PASSWORD, "\n".join(logs.output))
        a = attrs(r.cookie(SID))
        self.assertTrue(a["httponly"] and a["secure"])
        self.assertEqual(a["samesite"], "Strict")
        self.assertEqual(a["max-age"], str(web.SESSION_HOURS * 3600))
        ck = r.cookie(SID).split(";")[0]
        self.assertEqual(
            self.st.call("GET", "/api/bootstrap", cookie=ck).json()["user"],
            "admin:admin",
        )
        row = self.st.log_rows()[0]
        self.assertEqual(
            (row["action"], row["detail"], row["user"]),
            ("sign-in", "signed in (password)", "admin:admin"),
        )
        self.assertNotIn(PASSWORD, json.dumps(self.st.log_rows()))

    def test_accounts_file_and_cli(self):
        data = tempfile.mkdtemp()
        env = {**os.environ, "DATA_DIR": data}

        def cli(*args, stdin=""):
            return subprocess.run(
                [sys.executable, os.path.join(ROOT, "manage.py"), *args],
                input=stdin,
                capture_output=True,
                text=True,
                env=env,
                cwd=ROOT,
            )

        r = cli("set-password", "admin", stdin="short-pw-11\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("12", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(data, "accounts.json")))
        r = cli("set-password", "admin", stdin=PASSWORD + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        path = os.path.join(data, "accounts.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            raw = f.read()
        self.assertNotIn(PASSWORD, raw)
        acct = json.loads(raw)["admin"]
        self.assertEqual(set(acct), {"salt", "hash", "gen", "updated"})
        self.assertEqual(
            acct["hash"], auth.Auth.digest(PASSWORD, acct["salt"]), "scrypt hash"
        )
        self.assertEqual(cli("users").stdout.strip(), "admin")
        # The app (same user) reads what the CLI wrote.
        st = Stack(data_dir=data)
        self.addCleanup(st.close)
        r = st.call(
            "POST",
            "/api/login",
            body={"name": "admin", "password": PASSWORD},
            headers={"CF-Connecting-IP": "198.51.100.40"},
        )
        self.assertEqual(r.status, 200)
        self.assertEqual(cli("remove-user", "admin").returncode, 0)
        self.assertIn("No admin accounts", cli("users").stdout)
        self.assertNotEqual(cli("bogus").returncode, 0)
        # No web route creates, changes or lists accounts.
        ck = self.st.session()
        for method in ("GET", "POST", "PUT", "DELETE"):
            r = self.st.change(method, "/api/accounts", {"name": "x", "password": "y"})
            self.assertEqual(r.status, 404)
        self.assertEqual(self.st.call("GET", "/api/accounts", cookie=ck).status, 404)

    def test_password_off_without_accounts(self):
        self.assertEqual(self.st.web.auth.load(), {})
        self.assertFalse(self.st.call("GET", "/api/login-info").json()["password"])
        for name, pw in (("admin", PASSWORD), ("", ""), ("admin", "")):
            r = self.login(name, pw, "198.51.100.50")
            self.assertEqual(r.status, 401)
            self.assertIsNone(r.cookie(SID))
        self.st.web.auth.set_password("admin", PASSWORD)
        self.assertTrue(self.st.call("GET", "/api/login-info").json()["password"])


# ---------------------------------------------------------------- activity log
class ActivityTests(Base):
    def test_who_did_it(self):
        st = self.st
        r = st.change("POST", "/api/doors/event/action", {"action": "unlock_for"})
        self.assertEqual(r.status, 200, r.body)
        row = st.log_rows()[0]
        self.assertEqual((row["source"], row["user"], row["ok"]), ("web", JAKE, 1))
        st.mock.fail = True
        r = st.change("POST", "/api/doors/event/action", {"action": "lock"})
        self.assertEqual(r.status, 502)
        st.mock.fail = False
        row = st.log_rows()[0]
        self.assertEqual((row["user"], row["ok"]), (JAKE, 0))
        st.add_schedule()
        st.eng.fake_now = datetime(2027, 9, 6, 9, 0, tzinfo=TZ)
        st.eng.tick()
        row = st.log_rows()[0]
        self.assertTrue(row["source"].startswith("scheduler: "))
        self.assertEqual(row["user"], "")
        Bot({"allowed_chat_ids": [42]}, st.eng).handle(42, "/lock main gate")
        row = st.log_rows()[0]
        # 1.1.0 (AC39): Telegram rows name the chat, never empty.
        self.assertEqual((row["source"], row["user"]), ("telegram", "telegram:42/?"))
        api_rows = st.call("GET", "/api/log", cookie=st.session()).json()
        self.assertTrue(all("user" in r for r in api_rows))

    def test_schedule_changes_logged(self):
        st = self.st
        body = {
            "name": "Office hours",
            "doors": ["d2"],
            "days": [0],
            "start": "08:00",
            "end": "17:00",
        }
        sid = st.change("POST", "/api/schedules", body).json()["id"]
        st.change("PUT", "/api/schedules/%d" % sid, {**body, "end": "18:00"})
        st.change(
            "PUT", "/api/schedules/%d" % sid, {**body, "end": "18:00", "enabled": False}
        )
        st.change(
            "PUT", "/api/schedules/%d" % sid, {**body, "end": "18:00", "enabled": True}
        )
        st.change("PUT", "/api/schedules/%d" % sid, {**body, "name": "Office"})
        st.change("DELETE", "/api/schedules/%d" % sid)
        rows = [r for r in st.log_rows() if r["action"].startswith("schedule")]
        got = [(r["action"], r["detail"], r["user"]) for r in reversed(rows)]
        self.assertEqual(
            got,
            [
                ("schedule add", 'added schedule "Office hours"', JAKE),
                ("schedule edit", 'changed schedule "Office hours"', JAKE),
                ("schedule pause", 'paused schedule "Office hours"', JAKE),
                ("schedule resume", 'resumed schedule "Office hours"', JAKE),
                (
                    "schedule edit",
                    'changed schedule "Office hours" (now "Office")',
                    JAKE,
                ),
                ("schedule delete", 'deleted schedule "Office"', JAKE),
            ],
        )
        # A refused change writes nothing.
        n = len(st.log_rows())
        st.change("POST", "/api/schedules", {**body, "start": "nope"})
        st.change("DELETE", "/api/schedules/999")
        self.assertEqual(len(st.log_rows()), n)

    def test_signin_logged(self):
        self.st.google(JAKE)
        row = self.st.log_rows()[0]
        self.assertEqual(
            (row["door"], row["action"], row["detail"], row["user"]),
            ("", "sign-in", "signed in", JAKE),
        )
        n = len(self.st.log_rows())
        with self.assertLogs("doorcontrol", "WARNING"):
            self.st.google("someone@gmail.com")
        rows = self.st.log_rows()
        self.assertEqual(len(rows), n + 1)
        self.assertEqual((rows[0]["action"], rows[0]["ok"]), ("sign-in refused", 0))
        self.assertIn("someone@gmail.com", rows[0]["detail"])

    def test_migration_is_additive(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE schedules(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              doors TEXT NOT NULL, days TEXT, date TEXT, start TEXT NOT NULL, end TEXT NOT NULL,
              end_action TEXT NOT NULL DEFAULT 'reset', enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE door_state(door_id TEXT PRIMARY KEY, applied TEXT NOT NULL,
              end_action TEXT, since REAL, reason TEXT);
            CREATE TABLE log(id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL, door TEXT, action TEXT, source TEXT, ok INTEGER, detail TEXT);
            INSERT INTO schedules VALUES(5,'Office','["d2"]','[0,1]',NULL,'08:00','17:00','reset',1);
            INSERT INTO door_state VALUES('d2','unlock','reset',1.5,'Office');
            INSERT INTO log(id,ts,door,action,source,ok,detail)
              VALUES(41,1.0,'Office','lock','web',1,'locked now');
            INSERT INTO log(id,ts,door,action,source,ok,detail)
              VALUES(42,2.0,'Office','keep_unlock','scheduler: Office',1,'kept unlocked');
            """)
        c.commit()
        sched_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE name='schedules'"
        ).fetchone()[0]
        old_rows = [tuple(r) for r in c.execute("SELECT * FROM log ORDER BY id")]
        c.close()
        db = DB(path)
        DB(path)  # opening again is a no-op
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(log)")]
        self.assertEqual(
            cols,
            ["id", "ts", "door", "action", "source", "ok", "detail", "user", "kind"],
        )
        rows = [tuple(r) for r in db.conn.execute("SELECT * FROM log ORDER BY id")]
        self.assertEqual(rows, [r + ("", "door") for r in old_rows])
        self.assertEqual(db.get_schedule(5)["name"], "Office")
        self.assertEqual(db.get_state("d2")["applied"], "unlock")
        self.assertEqual(
            db.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='schedules'"
            ).fetchone()[0],
            sched_sql,
        )
        # The old version's INSERT (no user column) still works on the migrated file.
        db.conn.execute(
            "INSERT INTO log(ts,door,action,source,ok,detail) VALUES(3,'x','y','z',1,'')"
        )
        self.assertEqual(db.recent_log(1)[0]["user"], "")
        db.log("Office", "lock", "web", True, "locked now", user=JAKE)
        self.assertEqual(db.recent_log(1)[0]["user"], JAKE)

    def test_all_paths_log_and_cap(self):
        # Web and Telegram reach doors only through Engine.door_action.
        for name in ("web.py", "telegram_bot.py"):
            src = read("doorcontrol", name)
            self.assertNotRegex(src, r"\.client\.|set_lock_rule|\.unlock\(", name)
            self.assertIn("door_action(", src)
        # 1.1.0 (AC28): no row-count cap any more; rows are pruned by age (see
        # test_safety LogRetention).
        db = self.st.eng.db
        for i in range(1100):
            db.log("d", "lock", "web", True, str(i))
        n = db.conn.execute("SELECT COUNT(*) FROM log").fetchone()[0]
        self.assertEqual(n, 1100)
        self.assertEqual(db.recent_log(1)[0]["detail"], "1099")


# ---------------------------------------------------------------- independence
class IndependenceTests(Base):
    def test_tick_and_bot_without_session(self):
        st = self.st
        self.assertEqual(st.call("GET", "/api/doors").status, 401)  # nobody signed in
        st.add_schedule()
        st.eng.fake_now = datetime(2027, 9, 6, 9, 0, tzinfo=TZ)  # Monday
        st.eng.tick()
        self.assertEqual(st.mock.doors["d2"]["door_lock_relay_status"], "unlock")
        st.eng.fake_now = datetime(2027, 9, 6, 17, 0, tzinfo=TZ)
        st.eng.tick()
        self.assertEqual(st.mock.rules["d2"]["type"], "schedule")
        bot = Bot({"allowed_chat_ids": [42]}, st.eng)
        self.assertIn("unlocked for 120 min", bot.handle(42, "/unlock event center 2h"))
        self.assertIn("Not authorized", bot.handle(7, "/doors"))

    def test_scheduler_and_bot_do_not_import_sign_in(self):
        for name in (
            "engine.py",
            "telegram_bot.py",
            "ical.py",
            "unifi.py",
            "db.py",
            "notify.py",
            "people.py",
            "backup.py",
        ):
            tree = ast.parse(read("doorcontrol", name))
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mods.add(node.module or "")
                    mods |= {a.name for a in node.names}
                elif isinstance(node, ast.Import):
                    mods |= {a.name for a in node.names}
            with self.subTest(name):
                self.assertFalse(
                    mods & {"web", "auth", "google_signin"},
                    "%s imports %s" % (name, mods),
                )


# ---------------------------------------------------------------- security
CSP_PARTS = ("script-src 'self'", "style-src 'self'", "frame-ancestors 'none'")
INLINE = [
    re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>", re.I),
    re.compile(r"<style\b", re.I),
    re.compile(r"\sstyle\s*=", re.I),
    re.compile(r"\son[a-z]+\s*=", re.I),
]


class SecurityTests(Base):
    def test_headers_on_every_response(self):
        st = self.st
        ck = st.session()
        st.web.auth.set_password("admin", PASSWORD)
        for _ in range(10):
            st.call(
                "POST",
                "/api/login",
                body={"name": "admin", "password": "no"},
                headers={"CF-Connecting-IP": "198.51.100.99"},
            )

        def boom():
            raise RuntimeError("x")

        responses = {
            "401": st.call("GET", "/api/doors"),
            "403 host": st.call("GET", "/", "evil.test"),
            "403 csrf": st.call("POST", "/api/logout", cookie=ck),
            "404 cloudflare": st.call(
                "GET", "/", "x.test", headers={"CF-Connecting-IP": "1.2.3.4"}
            ),
            "404 route": st.call("GET", "/api/nope", cookie=ck),
            "429": st.call(
                "POST",
                "/api/login",
                body={"name": "admin", "password": "no"},
                headers={"CF-Connecting-IP": "198.51.100.99"},
            ),
            "health": st.call("GET", "/health"),
            "login page": st.call("GET", "/"),
            "static": st.call("GET", "/static/style.css"),
            "google start": st.call("GET", "/api/auth/google/start"),
            "google refused": st.callback("1.a", "1.b"),
            "dashboard": st.call("GET", "/", cookie=ck),
            "bootstrap": st.call("GET", "/api/bootstrap", cookie=ck),
            "unsupported method": st.call("PATCH", "/api/doors", cookie=ck),
        }
        st.eng.status = boom
        with self.assertLogs("doorcontrol", "ERROR"):
            responses["500"] = st.call("GET", "/api/doors", cookie=ck)
        self.assertEqual(responses["429"].status, 429)
        self.assertEqual(responses["500"].status, 500)
        for label, r in responses.items():
            with self.subTest(label, status=r.status):
                csp = r.header("Content-Security-Policy") or ""
                for part in CSP_PARTS:
                    self.assertIn(part, csp)
                self.assertEqual(r.header("X-Content-Type-Options"), "nosniff")
                self.assertEqual(r.header("Referrer-Policy"), "same-origin")
                self.assertEqual(r.header("X-Frame-Options"), "DENY")

    def test_no_inline_script_or_style(self):
        st = self.st
        pages = {
            "index.html": st.call("GET", "/", cookie=st.session()).body.decode(),
            "login.html": st.call("GET", "/").body.decode(),
            "error page": google_signin.page("x"),
            "signed-in page": google_signin.page(),
        }
        self.assertIn('id="doors"', pages["index.html"])
        for js in ("app.js", "login.js"):
            src = read("doorcontrol", "static", js)
            # The HTML the scripts build: every string and template literal.
            pages[js] = "\n".join(re.findall(r"`[^`]*`|\"[^\"\n]*\"|'[^'\n]*'", src))
            self.assertNotRegex(src, r"\.style\.|setAttribute\(\s*['\"]style", js)
            self.assertNotRegex(src, r"\beval\(|new Function\(", js)
        for label, html in pages.items():
            for pattern in INLINE:
                with self.subTest(label, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(html))

    def test_health_loopback_only(self):
        r = self.st.call("GET", "/health", "anything:1")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json(), {"ok": True, "version": web.VERSION})
        r = self.st.call("GET", "/health", headers={"CF-Connecting-IP": "1.2.3.4"})
        self.assertEqual(r.status, 404)
        self.assertEqual(self.st.call("POST", "/health").status, 404)
        for addr, ok in (
            ("127.0.0.1", True),
            ("::1", True),
            ("172.17.0.1", False),
            ("192.168.1.160", False),
        ):
            self.assertEqual(web.is_loopback(addr), ok, addr)


# ---------------------------------------------------------------- review round 1
def raw_request(st, lines, timeout=5):
    """Send exact header lines; return (status, body)."""
    with socket.create_connection(
        ("127.0.0.1", st.web.server_address[1]), timeout
    ) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), head.decode(), body


class TrustTests(Base):
    """S1/Q1: CF-Connecting-IP and the public Host count only from TRUSTED_PROXIES."""

    def untrusted(self):
        st = Stack(
            {"TRUSTED_PROXIES": "10.99.0.0/16"}
        )  # the test peer 127.0.0.1 is not in it
        self.addCleanup(st.close)
        return st

    def test_untrusted_peer_cannot_claim_cloudflare(self):
        st = self.untrusted()
        st.web.auth.set_password("admin", PASSWORD)
        cf = {"CF-Connecting-IP": "203.0.113.5"}
        # The public Host from a non-proxy peer is just an unknown host: refused, never open.
        self.assertEqual(st.call("GET", "/", HOST).status, 403)
        self.assertEqual(st.call("GET", "/api/auth/google/start", HOST).status, 403)
        r = st.call("POST", "/api/login", HOST, {"name": "admin", "password": PASSWORD})
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.cookie(SID))
        # A fake CF header on the LAN address is ignored (not remote).
        info = st.call("GET", "/api/login-info", LAN, headers=cf).json()
        self.assertFalse(info["remote"])

    def test_rotating_headers_from_non_proxy_peer_hit_429(self):
        st = self.untrusted()
        st.web.auth.set_password("admin", PASSWORD)
        for i in range(10):
            r = st.call(
                "POST",
                "/api/login",
                LAN if i % 2 else HOST,
                {"name": "admin", "password": "wrong"},
                headers={"CF-Connecting-IP": "203.0.113.%d" % i},
            )
            self.assertEqual(r.status, 403, i)
        # LAN host attempts reach the login check and count against the real peer.
        counted = sum(len(v) for v in st.web.limiter.by_address.values())
        self.assertEqual(set(st.web.limiter.by_address), {"127.0.0.1"})
        for i in range(10 - counted):
            st.call(
                "POST",
                "/api/login",
                LAN,
                {"name": "admin", "password": "wrong"},
                headers={"CF-Connecting-IP": "203.0.114.%d" % i},
            )
        r = st.call(
            "POST",
            "/api/login",
            LAN,
            {"name": "admin", "password": PASSWORD},
            headers={"CF-Connecting-IP": "203.0.115.1"},
        )
        self.assertEqual(r.status, 429, "11th attempt")

    def test_trusted_proxy_header_honored(self):
        self.st.web.auth.set_password("admin", PASSWORD)
        r = self.st.call(
            "POST",
            "/api/login",
            body={"name": "admin", "password": "wrong"},
            headers={"CF-Connecting-IP": "198.51.100.77"},
        )
        self.assertEqual(r.status, 401)
        self.assertIn("198.51.100.77", self.st.web.limiter.by_address)
        self.assertNotIn("127.0.0.1", self.st.web.limiter.by_address)

    def test_overall_cap(self):
        self.st.web.auth.set_password("admin", PASSWORD)
        for n in range(5):
            for _ in range(10):
                self.st.call(
                    "POST",
                    "/api/login",
                    body={"name": "admin", "password": "wrong"},
                    headers={"CF-Connecting-IP": "198.51.101.%d" % n},
                )
        r = self.st.call(
            "POST",
            "/api/login",
            body={"name": "admin", "password": PASSWORD},
            headers={"CF-Connecting-IP": "198.51.101.200"},
        )
        self.assertEqual(
            r.status, 429, "50 failures in total lock the password sign-in"
        )
        # Google sign-in is not affected.
        self.assertEqual(self.st.google(JAKE).status, 200)

    def test_limiter_prunes(self):
        lim = web.FailureLimiter()
        with mock.patch("time.time", return_value=1000.0):
            for i in range(30):
                lim.failed("10.0.0.%d" % i)
        self.assertEqual(len(lim.by_address), 30)
        with mock.patch("time.time", return_value=1000.0 + 901):
            self.assertTrue(lim.allowed("10.0.0.1"))
        self.assertEqual(lim.by_address, {})
        with mock.patch("time.time", return_value=5000.0):
            for i in range(500):
                lim.failed("10.1.%d.%d" % (i // 250, i % 250))
        self.assertLessEqual(lim.total(), 50, "bounded by the overall cap")

    def test_proxy_list(self):
        self.assertEqual(web.DEFAULT_TRUSTED_PROXIES, "172.31.248.1/32")
        nets = web.proxy_list(web.DEFAULT_TRUSTED_PROXIES)
        for addr, ok in (
            ("172.31.248.1", True),  # door-control-net gateway: cloudflared on the host
            ("172.31.248.2", False),  # the app's own container address
            ("172.17.0.1", False),  # default bridge gateway
            ("172.18.0.5", False),  # another fair app's container
            ("127.0.0.1", False),  # loopback is not trusted by default
            ("::1", False),
            ("192.168.1.50", False),  # fair-network PC
            ("nonsense", False),
        ):
            self.assertEqual(web.peer_in(addr, nets), ok, addr)
        lo = web.proxy_list("127.0.0.1/32")
        self.assertTrue(web.peer_in("::ffff:127.0.0.1", lo))
        with self.assertRaises(SystemExit):
            web.proxy_list("not-an-ip")
        self.assertEqual(web.proxy_list(""), [])

    def test_default_does_not_trust_other_peers(self):
        st = Stack({"TRUSTED_PROXIES": web.DEFAULT_TRUSTED_PROXIES})
        self.addCleanup(st.close)
        cf = {"CF-Connecting-IP": "203.0.113.9"}
        self.assertEqual(
            st.call("GET", "/api/login-info", HOST, headers=cf).status, 403
        )
        self.assertFalse(
            st.call("GET", "/api/login-info", LAN, headers=cf).json()["remote"]
        )


class RequestLimitTests(Base):
    def test_bad_content_length_is_400(self):
        st = self.st
        for value in ("-1", "abc", "-99999"):
            with self.subTest(value):
                status, _, body = raw_request(
                    st,
                    [
                        "POST /api/login HTTP/1.1",
                        "Host: " + HOST,
                        "Content-Type: application/json",
                        "Content-Length: " + value,
                        "Connection: close",
                    ],
                )
                self.assertEqual(status, 400)
                self.assertIn(b"error", body)
        cookie, token = st.with_csrf(st.session())
        status, _, _ = raw_request(
            st,
            [
                "POST /api/schedules HTTP/1.1",
                "Host: " + HOST,
                "Cookie: " + cookie,
                "X-CSRF-Token: " + token,
                "Content-Length: -1",
                "Connection: close",
            ],
        )
        self.assertEqual(status, 400)

    def test_idle_connection_times_out(self):
        self.assertEqual(web.Handler.timeout, 30)
        with mock.patch.object(web.Handler, "timeout", 0.5):
            s = socket.create_connection(("127.0.0.1", self.st.web.server_address[1]))
            s.settimeout(5)
            t = time.time()
            self.assertEqual(s.recv(10), b"")
            self.assertLess(time.time() - t, 4)
            s.close()

    def test_connection_cap(self):
        self.assertEqual(web.MAX_CONNECTIONS, 64)
        st = Stack(max_connections=2)
        self.addCleanup(st.close)
        port = st.web.server_address[1]
        with mock.patch.object(web.Handler, "timeout", 5):
            held = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
            time.sleep(0.3)
            extra = socket.create_connection(("127.0.0.1", port))
            extra.settimeout(2)
            t = time.time()
            self.assertEqual(extra.recv(10), b"", "closed at once")
            self.assertLess(time.time() - t, 1.5)
            extra.close()
            for s in held:
                s.close()
            time.sleep(0.3)
            self.assertEqual(st.call("GET", "/api/login-info").status, 200)

    def test_other_methods_get_plain_json(self):
        ck = self.st.session()
        for method in ("PATCH", "OPTIONS"):
            r = self.st.call(method, "/api/doors", cookie=ck)
            self.assertEqual(r.status, 405)
            self.assertEqual(
                r.json(), {"error": "This kind of request is not supported."}
            )
            self.assertIn("script-src 'self'", r.header("Content-Security-Policy"))
        r = self.st.call("HEAD", "/")
        self.assertEqual((r.status, r.body), (405, b""))
        self.assertIn("frame-ancestors 'none'", r.header("Content-Security-Policy"))
        r = self.st.call("TRACE", "/")
        self.assertEqual(r.status, 501)
        self.assertEqual(r.json(), {"error": "This kind of request is not supported."})
        self.assertEqual(r.header("X-Frame-Options"), "DENY")
        status, head, body = raw_request(self.st, ["GARBAGE"])
        self.assertEqual(status, 400)
        self.assertIn("Content-Security-Policy", head)
        self.assertNotIn(b"<html", body.lower())

    def test_unifi_errors_shown_plainly(self):
        self.st.mock.fail = True
        with self.assertLogs("doorcontrol", "WARNING"):
            r = self.st.change("POST", "/api/doors/event/action", {"action": "lock"})
        self.assertEqual(r.status, 502)
        self.assertNotIn(b"CODE_", r.body)
        self.assertNotIn(b"{\\", r.body)
        self.st.eng.last_error = "Traceback secret"
        h = self.st.call("GET", "/api/health", cookie=self.st.session()).json()
        self.assertNotIn("secret", h["scheduler_error"])


# ---------------------------------------------------------------- repo
# Copied unchanged from Awards (see CLAUDE.md "Shared code"). Changing them is a deliberate
# hash bump here and in CLAUDE.md after copying the newer Awards version.
SHARED = {
    "google_signin.py": "8b022dad96710068c35b4a578f361b5cd96e56a6f5d6947416d296c7cc179da1",
    "auth.py": "cf094dcf22d6a44d796f69fe3ce13abba68bc3d63579a10a50c6d5cba3a75cda",
}


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class RepoTests(unittest.TestCase):
    def test_shared_modules_unchanged(self):
        for name, digest in SHARED.items():
            with open(os.path.join(ROOT, "doorcontrol", name), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest, name)

    def test_no_web_password_anywhere(self):
        cfg = json.loads(read("config.example.json"))
        self.assertNotIn("web", cfg)
        self.assertNotIn("db_path", cfg)
        self.assertNotIn("password", read("config.example.json"))
        app_src = read("doorcontrol", "app.py")
        self.assertNotIn("web.password", app_src)
        self.assertNotIn('["password"]', app_src)
        self.assertNotIn('get("password")', app_src)
        src = read("doorcontrol", "web.py")
        self.assertNotIn("Basic", src)
        self.assertNotIn("WWW-Authenticate", src)

    def test_ignore_files_and_env_example(self):
        for f in (".gitignore", ".dockerignore"):
            lines = {l.strip() for l in read(f).splitlines()}
            with self.subTest(f):
                self.assertLessEqual({".env", "config.json", "data/"}, lines)
        env = {}
        for line in read(".env.example").splitlines():
            if line.strip() and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k] = v
        self.assertEqual(
            set(env),
            {
                "GOOGLE_CLIENT_ID",
                "GOOGLE_CLIENT_SECRET",
                "GOOGLE_ALLOWED_DOMAINS",
                "GOOGLE_ALLOWED_EMAILS",
                "PUBLIC_HOSTS",
                "ALLOWED_HOSTS",
                "TRUSTED_PROXIES",
            },
        )
        self.assertEqual(env["TRUSTED_PROXIES"], web.DEFAULT_TRUSTED_PROXIES)
        self.assertEqual(env["GOOGLE_CLIENT_ID"], "")
        self.assertEqual(env["GOOGLE_CLIENT_SECRET"], "")
        # 1.2.0 (AC41): admins only; no domain-wide access.
        self.assertEqual(env["GOOGLE_ALLOWED_EMAILS"], "")
        self.assertEqual(env["GOOGLE_ALLOWED_DOMAINS"], "")
        self.assertEqual(env["PUBLIC_HOSTS"], "doors.jakemc.me")
        hosts = env["ALLOWED_HOSTS"].split(",")
        for h in ("192.168.1.160:8480", "localhost:8480", "127.0.0.1:8480"):
            self.assertIn(h, hosts)
        compose = read("compose.yaml")
        for k in env:
            self.assertIn("%s: ${%s:-" % (k, k), compose)

    def test_claude_md_pins_shared_modules(self):
        claude = read("CLAUDE.md")
        for name, digest in SHARED.items():
            self.assertRegex(claude, re.escape(name) + r".*" + digest)

    def test_version_everywhere(self):
        v = read("VERSION").strip()
        self.assertRegex(v, r"^\d+\.\d+\.\d+$")
        self.assertEqual(v, web.VERSION)
        self.assertIn("image: door-control:%s\n" % v, read("compose.yaml"))
        self.assertEqual(
            read("README.md").splitlines()[0], "# Fairgrounds Door Control " + v
        )
        first = [l for l in read("CHANGELOG.md").splitlines() if l.startswith("## ")][0]
        self.assertTrue(first.startswith("## " + v + " "), first)
        self.assertIn("Current version: **%s**" % v, read("CLAUDE.md"))
        docker = read("Dockerfile")
        self.assertRegex(docker, r"(?m)^COPY run\.py manage\.py VERSION \./$")
        self.assertRegex(docker, r"(?m)^COPY doorcontrol \./doorcontrol$")
        top = {
            f
            for f in os.listdir(ROOT)
            if f.endswith(".py") and f not in ("mock_unifi.py",)
        }
        self.assertEqual(
            top, {"run.py", "manage.py"}, "new top-level file: add it to COPY"
        )

    def test_readme_documents_sign_everyone_out(self):
        readme = read("README.md")
        self.assertIn("Sign everyone out", readme)
        self.assertIn("manage.py revoke --all", readme)
        self.assertIn("SESSION_HOURS", readme)
        self.assertNotIn("SESSION_DAYS", readme)

    def test_readme_has_no_tailscale_or_password_text(self):
        readme = read("README.md")
        for gone in ("Tailscale", "web.password", "tailscale", "port-forward"):
            self.assertNotIn(gone, readme)
        for needed in (
            "If you cannot sign in",
            "long random password",
            "UniFi Access app",
            "GOOGLE_CLIENT_ID",
            "https://doors.jakemc.me/api/auth/google/callback",
            "Restore",
            "docker compose exec app python manage.py",
        ):
            self.assertIn(needed, readme)

    def test_compose_hardening(self):
        compose = read("compose.yaml")
        for needed in (
            "cap_drop:",
            "- ALL",
            "pids_limit:",
            "mem_limit:",
            "read_only: true",
        ):
            self.assertIn(needed, compose)

    def test_server_blocks_are_valid_bash_and_use_no_image_tag(self):
        for name in ("CLAUDE.md", "README.md"):
            text = read(name)
            self.assertNotIn(
                "door-control:1.0.0", text.replace("image: door-control", "")
            )
            for i, block in enumerate(re.findall(r"```bash\n(.*?)```", text, re.S)):
                with self.subTest(name, block=i):
                    r = subprocess.run(
                        ["bash", "-n"], input=block, capture_output=True, text=True
                    )
                    self.assertEqual(r.returncode, 0, r.stderr)

    def test_first_move_failure_paths_restore_the_old_service(self):
        claude = read("CLAUDE.md")
        block = claude.split("### First move")[1].split("```bash\n")[1].split("```")[0]
        after_stop = block.split("sudo systemctl stop door-control")[1]
        before_ok = after_stop.split("trap - ERR")[0]
        # Every explicit failure after the stop goes through restore_old.
        for line in before_ok.splitlines():
            if line.startswith("if ") and "exit 1" in line:
                self.assertIn("restore_old", line)
        self.assertIn("set -eE", block)
        self.assertIn("trap restore_old ERR", block)
        self.assertIn(
            "Health.Status", before_ok, "rollback if the container is not healthy"
        )
        self.assertIn(
            "sqlite3", block, "database copied with SQLite's backup (WAL safe)"
        )

    def test_compose_network_matches_trusted_proxy(self):
        compose = read("compose.yaml")
        self.assertIn("name: door-control-net", compose)
        self.assertIn("subnet: 172.31.248.0/29", compose)
        self.assertIn("gateway: 172.31.248.1\n", compose)
        self.assertIn("${TRUSTED_PROXIES:-%s}" % web.DEFAULT_TRUSTED_PROXIES, compose)
        self.assertNotIn("0.0.0.0", compose)
        for name in ("README.md", "CLAUDE.md"):
            self.assertIn("door-control-net", read(name))
            self.assertNotIn("172.16.0.0/12", read(name))
        self.assertIn(
            "0.0.0.0:8480:8480", read("README.md"), "how to re-publish, with warning"
        )
        self.assertIn("docker network inspect", read("README.md"))

    def test_deploy_steps_pin_the_unifi_certificate(self):
        claude = read("CLAUDE.md")
        block = claude.split("### First move")[1].split("```bash\n")[1].split("```")[0]
        self.assertIn("openssl s_client", block)
        self.assertIn('c["unifi"]["cert_sha256"]', block)
        # Read before the old service stops, so a failure changes nothing.
        self.assertLess(block.index("openssl"), block.index("systemctl stop"))
        self.assertIn("cert_sha256 is not set", claude.split("after a PR merges")[1])
        readme = read("README.md")
        pin = readme.split("### Pin the UniFi console certificate")[1].split(
            "```bash\n"
        )[1]
        self.assertIn("openssl s_client", pin)
        self.assertIn('["unifi"]["host"]', pin, "console address read from config.json")

    def test_compose_publishes_on_loopback_only(self):
        compose = read("compose.yaml")
        ports = re.findall(r'(?m)^\s*- "([^"]*:8480)"', compose)
        self.assertEqual(ports, ["127.0.0.1:8480:8480"])
        self.assertNotIn("network_mode", compose)
        readme = read("README.md")
        self.assertIn("reachable only through **https://doors.jakemc.me**", readme)
        self.assertIn("cannot connect", readme)
        self.assertIn("URL **`127.0.0.1:8480`**", readme)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "run.sh")))

    def test_pin_block_handles_ipv6_console(self):
        for name in ("README.md", "CLAUDE.md"):
            text = read(name)
            snippet = re.search(r"CONSOLE=\$\(python3 -c '([^']*)'", text).group(1)
            for host, want in (
                ("https://192.168.1.1:12445", "192.168.1.1:12445"),
                ("https://[fd00::1]:12445", "[fd00::1]:12445"),
                ("https://console.local", "console.local:443"),
            ):
                with self.subTest(name, host=host):
                    cfg = os.path.join(tempfile.mkdtemp(), "c.json")
                    with open(cfg, "w") as f:
                        json.dump({"unifi": {"host": host}}, f)
                    out = subprocess.run(
                        [sys.executable, "-c", snippet, cfg],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout.strip()
                    self.assertEqual(out, want)


if __name__ == "__main__":
    unittest.main()
