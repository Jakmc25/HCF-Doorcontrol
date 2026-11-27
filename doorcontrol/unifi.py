"""Minimal client for the UniFi Access local Open API (port 12445)."""

import json
import ssl
import urllib.error
import urllib.request


class UnifiError(Exception):
    pass


class UnifiClient:
    def __init__(self, host, token, verify_tls=False, timeout=10):
        self.host = host.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ctx = None
        if self.host.startswith("https"):
            # The console uses a self-signed certificate by default.
            self.ctx = (
                ssl.create_default_context()
                if verify_tls
                else ssl._create_unverified_context()
            )

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.host + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout, context=self.ctx
            ) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            raise UnifiError("UniFi returned HTTP %s: %s" % (e.code, detail))
        except (urllib.error.URLError, OSError) as e:
            raise UnifiError("Cannot reach UniFi Access: %s" % getattr(e, "reason", e))
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            raise UnifiError("UniFi returned invalid JSON")
        if payload.get("code") not in (None, "SUCCESS"):
            raise UnifiError(
                "UniFi error %s: %s" % (payload.get("code"), payload.get("msg"))
            )
        return payload.get("data")

    def doors(self):
        return self._req("GET", "/api/v1/developer/doors") or []

    def lock_rule(self, door_id):
        return self._req("GET", "/api/v1/developer/doors/%s/lock_rule" % door_id) or {}

    def set_lock_rule(self, door_id, rule_type, interval=None):
        body = {"type": rule_type}
        if interval is not None:
            body["interval"] = int(interval)
        return self._req("PUT", "/api/v1/developer/doors/%s/lock_rule" % door_id, body)

    def unlock(self, door_id):
        """Momentary remote unlock (same as pressing the unlock button)."""
        return self._req(
            "PUT",
            "/api/v1/developer/doors/%s/unlock" % door_id,
            {"actor_name": "Door Control"},
        )
