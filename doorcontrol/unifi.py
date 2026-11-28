"""Minimal client for the UniFi Access local Open API (port 12445)."""

import hashlib
import http.client
import json
import logging
import ssl
import urllib.error
import urllib.request

log = logging.getLogger("doorcontrol")


class UnifiError(Exception):
    pass


class UnifiUnreachable(UnifiError):
    """The console did not answer at all (down, timeout, network). The scheduler skips the
    rest of that tick's UniFi calls so a dead console cannot stretch one tick for minutes.
    """


class CertificateMismatch(OSError):
    pass


def normalize_fingerprint(value):
    """'sha256 Fingerprint=AB:CD:...' or 'ab cd ...' -> 'abcd...' (64 hex characters)."""
    v = str(value or "").strip()
    if "=" in v:
        v = v.split("=", 1)[1]
    v = "".join(c for c in v if c not in ": \t").lower()
    if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
        raise ValueError(
            "unifi.cert_sha256 must be the console certificate's SHA-256 fingerprint "
            "(64 hex characters, colons allowed)."
        )
    return v


def cert_matches(der_bytes, fingerprint):
    return hashlib.sha256(der_bytes or b"").hexdigest() == fingerprint


def _pinned_handler(context, fingerprint):
    """HTTPS handler that checks the console's certificate BEFORE the request (and its
    bearer token) is sent."""

    class PinnedConnection(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            der = self.sock.getpeercert(binary_form=True)
            if not cert_matches(der, fingerprint):
                self.sock.close()
                raise CertificateMismatch(
                    "certificate fingerprint does not match unifi.cert_sha256"
                )

    class PinnedHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(PinnedConnection, req, context=context)

    return PinnedHandler(context=context)


class UnifiClient:
    def __init__(self, host, token, verify_tls=False, timeout=10, cert_sha256=None):
        self.host = host.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ctx = None
        self.opener = None
        self.pinned = False
        if self.host.startswith("https"):
            # The console uses a self-signed certificate by default.
            self.ctx = (
                ssl.create_default_context()
                if verify_tls
                else ssl._create_unverified_context()
            )
            if cert_sha256:
                fp = normalize_fingerprint(cert_sha256)
                self.opener = urllib.request.build_opener(_pinned_handler(self.ctx, fp))
                self.pinned = True

    def _open(self, req):
        if self.opener:
            return self.opener.open(req, timeout=self.timeout)
        return urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)

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
        # Details go to the server log; the sentence in UnifiError is shown to people.
        try:
            with self._open(req) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            log.warning("UniFi HTTP %s on %s %s: %s", e.code, method, path, detail)
            raise UnifiError(
                "The UniFi door controller refused the request (HTTP %s)." % e.code
            )
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            log.warning("UniFi unreachable on %s %s: %s", method, path, reason)
            if isinstance(reason, CertificateMismatch):
                raise UnifiError(
                    "The UniFi console's certificate does not match unifi.cert_sha256, "
                    "so Door Control did not connect."
                )
            raise UnifiUnreachable("Cannot reach the UniFi door controller.")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            log.warning("UniFi sent invalid JSON on %s %s", method, path)
            raise UnifiError("The UniFi door controller sent an unreadable answer.")
        if payload.get("code") not in (None, "SUCCESS"):
            log.warning(
                "UniFi error on %s %s: %s %s",
                method,
                path,
                payload.get("code"),
                payload.get("msg"),
            )
            raise UnifiError(
                "The UniFi door controller reported an error (%s)."
                % payload.get("code")
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
