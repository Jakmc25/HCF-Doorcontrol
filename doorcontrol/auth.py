"""Name/password (admin) sign-in, on the fair network and through the Cloudflare tunnel.
Accounts live in DATA/accounts.json as scrypt hashes; manage.py adds, changes and removes them. Changing a password signs that person out everywhere.
"""

import os, json, time, hmac, hashlib, secrets, base64, threading
from pathlib import Path

DAYS = 14
_lock = threading.Lock()
_fails = {}


class Auth:
    def __init__(self, data_dir, secret, cookie):
        self.file = Path(data_dir) / "accounts.json"
        self.secret = secret
        self.cookie = cookie

    def load(self):
        try:
            return json.loads(self.file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def save(self, accounts):
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(accounts, f)
        os.replace(tmp, self.file)

    @staticmethod
    def name(v):
        v = " ".join(str(v or "").split()).lower()
        if not 1 <= len(v) <= 40 or not all(c.isalnum() or c in " ._-@" for c in v):
            raise ValueError("Use a name of up to 40 letters or numbers.")
        return v

    @staticmethod
    def digest(password, salt):
        return hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=2**14,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
        ).hex()

    def set_password(self, name, password):
        name = self.name(name)
        if len(password) < 10:
            raise ValueError("Use a password of at least 10 characters.")
        salt = secrets.token_hex(16)
        a = self.load()
        a[name] = {
            "salt": salt,
            "hash": self.digest(password, salt),
            "gen": secrets.token_hex(8),
            "updated": int(time.time()),
        }
        self.save(a)
        return name

    def remove(self, name):
        a = self.load()
        name = self.name(name)
        if a.pop(name, None) is None:
            raise ValueError("No account called " + name + ".")
        self.save(a)

    def check(self, name, password):
        try:
            name = self.name(name)
        except ValueError:
            return None
        acct = self.load().get(name)
        # Hash even for unknown names so the response time doesn't reveal which names exist.
        got = self.digest(str(password or ""), acct["salt"] if acct else "00" * 16)
        return name if acct and hmac.compare_digest(got, acct["hash"]) else None

    def issue(self, name):
        exp = str(int(time.time()) + DAYS * 86400)
        gen = self.load()[name]["gen"]
        n = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
        return f'{n}.{exp}.{hmac.new(self.secret,f"{n}.{exp}.{gen}".encode(),hashlib.sha256).hexdigest()}'

    def verify(self, value):
        try:
            n, exp, mac = value.split(".")
            name = base64.urlsafe_b64decode(n + "=" * (-len(n) % 4)).decode()
            acct = self.load().get(name)
            if not acct or int(exp) < time.time():
                return None
            return (
                name
                if hmac.compare_digest(
                    mac,
                    hmac.new(
                        self.secret, f"{n}.{exp}.{acct['gen']}".encode(), hashlib.sha256
                    ).hexdigest(),
                )
                else None
            )
        except Exception:
            return None

    def cookie_header(self, value, clear=False, secure=True):
        # Not Secure on the fair network, which is plain HTTP, or the browser would never send it back.
        return f"{self.cookie}={'' if clear else value}; HttpOnly;{' Secure;' if secure else ''} SameSite=Strict; Path=/; Max-Age={0 if clear else DAYS*86400}"

    @staticmethod
    def allowed(ip):
        """At most 10 failed sign-ins per address in 15 minutes."""
        with _lock:
            now = time.time()
            _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 900]
            return len(_fails[ip]) < 10

    @staticmethod
    def failed(ip):
        with _lock:
            _fails.setdefault(ip, []).append(time.time())
