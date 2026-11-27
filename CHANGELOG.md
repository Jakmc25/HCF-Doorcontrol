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
- `web.password`, `web.bind`, `web.port` and `db_path` removed from `config.json` (ignored with a warning).
