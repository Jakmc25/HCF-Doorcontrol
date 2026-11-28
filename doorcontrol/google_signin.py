"""Google sign-in for use away from the fair network (through the Cloudflare tunnel). The name/password sign-in in auth.py stays as the admin fallback.
Settings come from .env (never from git): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and who may sign in, GOOGLE_ALLOWED_DOMAINS (e.g. hardincountyfair.org) and/or GOOGLE_ALLOWED_EMAILS. With neither set nobody can sign in with Google.
Google sends people back to https://<public hostname>/api/auth/google/callback, which must be listed as an authorized redirect URI on the OAuth client.
"""

import os, json, time, html, hmac, hashlib, secrets, base64, urllib.parse, urllib.request, urllib.error

AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
CALLBACK = "/api/auth/google/callback"
STATE_COOKIE = "google_state"
DAYS = 14


def _list(var):
    return sorted(
        {v.strip().lower() for v in os.environ.get(var, "").split(",") if v.strip()}
    )


def _b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Google:
    def __init__(self, secret, cookie):
        self.secret = secret
        self.cookie = cookie
        self.client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        self.domains = _list("GOOGLE_ALLOWED_DOMAINS")
        self.emails = _list("GOOGLE_ALLOWED_EMAILS")
        # Changing who is allowed signs out every Google session made under the old rules.
        self.policy = hashlib.sha256(
            json.dumps([self.domains, self.emails]).encode()
        ).hexdigest()[:16]

    def ready(self):
        return bool(
            self.client_id and self.client_secret and (self.domains or self.emails)
        )

    def allowed(self, email):
        email = str(email or "").lower()
        return email in self.emails or email.rpartition("@")[2] in self.domains

    def mac(self, v):
        return hmac.new(self.secret, v.encode(), hashlib.sha256).hexdigest()

    def start(self, host):
        """-> (Google URL, Set-Cookie). The state cookie ties the callback to this browser; the nonce ties Google's answer to this request."""
        state = str(int(time.time())) + "." + secrets.token_urlsafe(24)
        q = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": f"https://{host}{CALLBACK}",
            "state": state,
            "nonce": self.mac("nonce." + state),
            "prompt": "select_account",
        }
        if len(self.domains) == 1 and not self.emails:
            q["hd"] = self.domains[0]
        # Lax, not Strict: the browser must send it when Google redirects back.
        return (
            AUTHORIZE + "?" + urllib.parse.urlencode(q),
            f"{STATE_COOKIE}={state}; HttpOnly; Secure; SameSite=Lax; Path=/api/auth/google/; Max-Age=600",
        )

    def finish(self, host, query, state_cookie):
        """Swap Google's one-time code for the person's verified email. Raises PermissionError with a message to show."""
        if query.get("error"):
            raise PermissionError("Google sign-in was cancelled.")
        state = query.get("state") or ""
        if (
            not state
            or not state_cookie
            or not hmac.compare_digest(state, state_cookie)
            or time.time() - int(state.split(".")[0]) > 600
        ):
            raise PermissionError(
                "That sign-in expired or was started in another browser. Try again."
            )
        body = urllib.parse.urlencode(
            {
                "code": query.get("code") or "",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": f"https://{host}{CALLBACK}",
                "grant_type": "authorization_code",
            }
        ).encode()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(TOKEN, data=body, method="POST"), timeout=20
            ) as r:
                tok = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print("google token error", e.code, e.read()[:300], flush=True)
            raise PermissionError("Google did not accept the sign-in. Try again.")
        except urllib.error.URLError:
            raise PermissionError(
                "The server could not reach Google. Try again in a minute."
            )
        # The ID token came straight from Google's token endpoint over verified HTTPS, which OpenID Connect accepts in place of a signature check.
        try:
            claims = json.loads(_unb64(tok["id_token"].split(".")[1]))
        except Exception:
            raise PermissionError("Google did not say who you are. Try again.")
        if (
            claims.get("iss")
            not in ("https://accounts.google.com", "accounts.google.com")
            or claims.get("aud") != self.client_id
            or int(claims.get("exp", 0)) < time.time()
            or not hmac.compare_digest(
                str(claims.get("nonce", "")), self.mac("nonce." + state)
            )
        ):
            raise PermissionError(
                "Google’s answer didn’t match this sign-in. Try again."
            )
        email = str(claims.get("email") or "").lower()
        if not email or claims.get("email_verified") is not True:
            raise PermissionError("Your Google account’s email address isn’t verified.")
        # Google only vouches for an address on a Workspace domain when the token names that domain (hd); without it the account may be a personal one that once verified the address.
        domain = email.rpartition("@")[2]
        if not (
            email in self.emails
            or (domain in self.domains and claims.get("hd") == domain)
        ):
            print("google sign-in refused", email, flush=True)
            raise PermissionError(
                f"{email} isn’t allowed to use this app. Ask Jake to add it."
            )
        return email

    def issue(self, email):
        n = _b64(email)
        exp = str(int(time.time()) + DAYS * 86400)
        return f'g.{n}.{exp}.{self.mac(f"g.{n}.{exp}.{self.policy}")}'

    def verify(self, value):
        try:
            g, n, exp, mac = value.split(".")
            email = _unb64(n).decode()
            if g != "g" or int(exp) < time.time() or not self.allowed(email):
                return None
            return (
                email
                if hmac.compare_digest(mac, self.mac(f"g.{n}.{exp}.{self.policy}"))
                else None
            )
        except Exception:
            return None

    def cookie_header(self, value):
        return f"{self.cookie}={value}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={DAYS*86400}"


def page(message=None):
    """The page Google's callback lands on. After sign-in it moves on to the app from this same site, so the browser sends the SameSite=Strict cookie it was just given."""
    if message is None:
        return '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/"><title>Signed in</title><p><a href="/">Continue</a></p>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Sign-in problem</title><link rel="stylesheet" href="/static/style.css"></head>'
        f'<body><main class="loading"><div class="card login-card"><img src="/static/fair-logo.png" alt="Hardin County Fair" width="150"><h1>Sign-in problem</h1><p class="error-text">{html.escape(message)}</p><a class="button primary big" href="/">Back to sign in</a></div></main></body></html>'
    )
