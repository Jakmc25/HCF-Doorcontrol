# Changelog

## 1.0.0 (2026-11-27)
First numbered release.
- Google sign-in (verified hardincountyfair.org accounts) at https://doors.jakemc.me replaces the
  shared dashboard password. The fair network needs sign-in too. Break-glass admin password from
  `manage.py set-password`, rate limited, shown only when an account exists.
- Activity log records who did it: email on door actions, schedule changes (now logged) and sign-ins.
  Additive database migration (`log.user`).
- Runs in Docker Compose (read-only container, non-root, volume `door_control_data`), replacing the
  systemd service (kept only for rollback).
- Security: strict CSP and security headers on every response, CSRF token on every change, Cloudflare
  traffic for other hostnames refused, plain error messages.
- Code formatted with black and prettier; JS and CSS moved out of `index.html`.
- Review round 3: the port is published on 127.0.0.1 only; the dashboard is reachable only through
  https://doors.jakemc.me (Cloudflare tunnel URL `127.0.0.1:8480`). Pin block handles IPv6 consoles.
- Review round 2: the app gets its own Docker network `door-control-net` (172.31.248.0/29) and only
  its gateway (172.31.248.1, where cloudflared on fair-server appears) is trusted; deploy steps pin the
  UniFi console certificate.
- Review round 1: Cloudflare headers and the public hostname count only from `TRUSTED_PROXIES`;
  break-glass failures capped at 10 per address and 50 in total per 15 minutes; bad Content-Length
  refused; 30-second connection timeout and at most 64 connections; optional `unifi.cert_sha256`
  certificate pinning; plain error sentences (details in the server log); calendar feed must be https://
  and at most 5 MB; container drops all capabilities with pid and memory limits; admin sign-ins shown as
  `admin:NAME`; deploy block restores the old service on every failure.
- `web.password`, `web.bind`, `web.port` and `db_path` removed from `config.json` (ignored with a warning).
