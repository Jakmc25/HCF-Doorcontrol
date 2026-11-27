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
    "GOOGLE_ALLOWED_DOMAINS": "hardincountyfair.org",
    "GOOGLE_ALLOWED_EMAILS": "",
}
JAKE = "jake@hardincountyfair.org"
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

    def __init__(self, env=None, data_dir=None):
        self.mock = mock_unifi.Mock()
        self.usrv = mock_unifi.make_server(self.mock)
        client = UnifiClient(
            "http://127.0.0.1:%d" % self.usrv.server_address[1], mock_unifi.TOKEN
        )
        self.data = data_dir or tempfile.mkdtemp()
        db = DB(os.path.join(self.data, "doorcontrol.db"))
        self.eng = Clock({"timezone": "America/New_York"}, db, client)
        with mock.patch.dict(os.environ, {**ENV, **(env or {})}):
            self.web = web.make_server(self.eng, self.data, "127.0.0.1", 0)
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
    def session(self, email=JAKE):
        return "doors_auth=" + self.web.google.issue(email)

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
                        self.assertIsNone(r.cookie("doors_auth"))
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
        g = self.st.web.google
        good = g.issue(JAKE)
        n = base64.urlsafe_b64encode(JAKE.encode()).decode().rstrip("=")
        exp = str(int(time.time()) - 1)
        expired = f"g.{n}.{exp}.{g.mac(f'g.{n}.{exp}.{g.policy}')}"
        with mock.patch.dict(os.environ, {"GOOGLE_ALLOWED_DOMAINS": "example.org"}):
            other_policy = google_signin.Google(self.st.web.secret, "doors_auth")
        wrong_policy = other_policy.issue(JAKE)
        other = google_signin.Google(b"another-secret", "doors_auth")
        self.st.web.auth.set_password("admin", PASSWORD)
        pw = self.st.web.auth.issue("admin")
        bad = {
            "forged": f"g.{n}.9999999999." + "0" * 64,
            "tampered mac": good[:-1] + ("0" if good[-1] != "0" else "1"),
            "swapped email": good.replace(
                n, base64.urlsafe_b64encode(b"ann@hardincountyfair.org").decode()
            ),
            "expired": expired,
            "wrong policy": wrong_policy,
            "other secret": other.issue(JAKE),
            "password tampered": pw[:-1] + ("0" if pw[-1] != "0" else "1"),
            "garbage": "x",
        }
        self.assertEqual(
            self.st.call("GET", "/api/doors", cookie="doors_auth=" + good).status, 200
        )
        for label, value in bad.items():
            with self.subTest(label):
                r = self.st.call("GET", "/api/doors", cookie="doors_auth=" + value)
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
                    self.assertIsNone(r.cookie("doors_auth"))
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
        self.assertIsNone(r.cookie("doors_auth"))

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
        ck = r.cookie("doors_auth").split(";")[0]
        self.assertTrue(ck.startswith("doors_auth=g."))
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
        before = st.log_rows()
        for label, attempt in cases.items():
            with self.subTest(label):
                with self.assertLogs("doorcontrol", "WARNING") as logs:
                    r = attempt()
                self.assertEqual(r.status, 403)
                self.assertIsNone(r.cookie("doors_auth"))
                self.assertIn(b"Sign-in problem", r.body)
                self.assertNotIn(b"Traceback", r.body)
                self.assertTrue(
                    any("google sign-in refused" in line for line in logs.output)
                )
        self.assertEqual(st.log_rows(), before, "refusals go to the server log only")

    def test_outside_account_refused_without_allowed_emails(self):
        self.assertEqual(ENV["GOOGLE_ALLOWED_EMAILS"], "")
        self.assertEqual(self.st.web.google.emails, [])
        with self.assertLogs("doorcontrol", "WARNING"):
            r = self.st.google("volunteer@gmail.com")
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.cookie("doors_auth"))

    def test_allowed_domain_change_ends_sessions(self):
        ck = self.st.google(JAKE).cookie("doors_auth").split(";")[0]
        self.assertEqual(self.st.call("GET", "/api/doors", cookie=ck).status, 200)
        # Restart with the same settings: the session survives (secret is on disk).
        same = Stack(data_dir=self.st.data)
        self.addCleanup(same.close)
        self.assertEqual(same.call("GET", "/api/doors", cookie=ck).status, 200)
        # Restart with a different allowed domain: old sessions end.
        changed = Stack(
            {"GOOGLE_ALLOWED_DOMAINS": "hardincountyfair.org,example.org"},
            data_dir=self.st.data,
        )
        self.addCleanup(changed.close)
        self.assertEqual(changed.call("GET", "/api/doors", cookie=ck).status, 401)

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
        a = attrs(self.st.google(JAKE).cookie("doors_auth"))
        self.assertTrue(a["httponly"] and a["secure"])
        self.assertEqual(a["samesite"], "Strict")
        self.assertEqual(a["path"], "/")
        self.assertEqual(a["max-age"], str(14 * 86400))
        r = self.st.call("GET", "/api/bootstrap", cookie=self.st.session())
        a = attrs(r.cookie("doors_session"))
        self.assertTrue(a["httponly"] and a["secure"])
        self.assertEqual(a["samesite"], "Strict")

    def test_sign_out_needs_csrf_and_clears_cookie(self):
        r = self.st.call("POST", "/api/logout", cookie=self.st.session())
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.cookie("doors_auth"))
        r = self.st.change("POST", "/api/logout")
        self.assertEqual(r.status, 200)
        c = r.cookie("doors_auth")
        self.assertTrue(c.startswith("doors_auth=;"))
        a = attrs(c)
        self.assertEqual(a["max-age"], "0")
        self.assertTrue(a["secure"] and a["httponly"])

    def test_lifetime_constant_and_expiry(self):
        self.assertEqual(web.SESSION_DAYS, 14)
        self.assertEqual(google_signin.DAYS, web.SESSION_DAYS)
        self.assertEqual(auth.DAYS, web.SESSION_DAYS)
        # make_server is what sets both modules from the one constant.
        with mock.patch.object(web, "SESSION_DAYS", 3):
            st = Stack()
            self.addCleanup(st.close)
            self.assertEqual((google_signin.DAYS, auth.DAYS), (3, 3))
            a = attrs(st.google(JAKE).cookie("doors_auth"))
            self.assertEqual(a["max-age"], str(3 * 86400))
        Stack().close()  # back to 14
        self.assertEqual((google_signin.DAYS, auth.DAYS), (14, 14))
        g, au = self.st.web.google, self.st.web.auth
        au.set_password("admin", PASSWORD)
        t0 = 2_000_000_000
        with mock.patch("time.time", return_value=t0):
            gv, pv = g.issue(JAKE), au.issue("admin")
        end = t0 + 14 * 86400
        with mock.patch("time.time", return_value=end):
            self.assertEqual(g.verify(gv), JAKE)
            self.assertEqual(au.verify(pv), "admin")
        with mock.patch("time.time", return_value=end + 1):
            self.assertIsNone(g.verify(gv))
            self.assertIsNone(au.verify(pv))


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
        self.assertIsNone(r.cookie("doors_auth"))
        self.assertEqual(self.login("admin", PASSWORD, "198.51.100.18").status, 200)

    def test_password_signin_and_log_row(self):
        self.st.web.auth.set_password("admin", PASSWORD)
        r = self.login("admin", "not it", "198.51.100.30")
        self.assertEqual(r.status, 401)
        self.assertEqual(r.json(), {"error": "Wrong name or password."})
        self.assertIsNone(r.cookie("doors_auth"))
        with self.assertLogs("doorcontrol", "INFO") as logs:
            r = self.login("Admin", PASSWORD, "198.51.100.31")
        self.assertEqual(r.status, 200)
        self.assertNotIn(PASSWORD, "\n".join(logs.output))
        a = attrs(r.cookie("doors_auth"))
        self.assertTrue(a["httponly"] and a["secure"])
        self.assertEqual(a["samesite"], "Strict")
        self.assertEqual(a["max-age"], str(web.SESSION_DAYS * 86400))
        ck = r.cookie("doors_auth").split(";")[0]
        self.assertEqual(
            self.st.call("GET", "/api/bootstrap", cookie=ck).json()["user"], "admin"
        )
        row = self.st.log_rows()[0]
        self.assertEqual(
            (row["action"], row["detail"], row["user"]),
            ("sign-in", "signed in (password)", "admin"),
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
            self.assertIsNone(r.cookie("doors_auth"))
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
        self.assertEqual((row["source"], row["user"]), ("telegram", ""))
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
        self.assertEqual(len(self.st.log_rows()), n)

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
            cols, ["id", "ts", "door", "action", "source", "ok", "detail", "user"]
        )
        rows = [tuple(r) for r in db.conn.execute("SELECT * FROM log ORDER BY id")]
        self.assertEqual(rows, [r + ("",) for r in old_rows])
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
        db = self.st.eng.db
        for i in range(1100):
            db.log("d", "lock", "web", True, str(i))
        n = db.conn.execute("SELECT COUNT(*) FROM log").fetchone()[0]
        self.assertEqual(n, 1000)
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
        for name in ("engine.py", "telegram_bot.py", "ical.py", "unifi.py", "db.py"):
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
            },
        )
        self.assertEqual(env["GOOGLE_CLIENT_ID"], "")
        self.assertEqual(env["GOOGLE_CLIENT_SECRET"], "")
        self.assertEqual(env["GOOGLE_ALLOWED_EMAILS"], "")
        self.assertEqual(env["GOOGLE_ALLOWED_DOMAINS"], "hardincountyfair.org")
        self.assertEqual(env["PUBLIC_HOSTS"], "doors.jakemc.me")
        hosts = env["ALLOWED_HOSTS"].split(",")
        for h in ("192.168.1.160:8480", "localhost:8480", "127.0.0.1:8480"):
            self.assertIn(h, hosts)
        compose = read("compose.yaml")
        for k in env:
            self.assertIn("%s: ${%s:-}" % (k, k), compose)


if __name__ == "__main__":
    unittest.main()
