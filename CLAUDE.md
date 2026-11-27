# HCF Door Control

## What this app does
Friendly control panel for the fairgrounds' UniFi Access doors: one-tap lock/unlock, repeating and
one-time schedules, Google Calendar events ("Unlock: Event Center") that unlock doors, and a Telegram
bot. It uses UniFi's temporary lock rules (`keep_unlock`, `reset`, `keep_lock`, `lock_now`, `custom`)
through the local Access Open API (`https://<console>:12445`, bearer token). It never edits UniFi's own
schedules or policies.

## Stack
Language/runtime: Python 3.9+ standard library only (http.server, sqlite3, urllib); plain JS front end.
Key libraries: none.
Layout: `doorcontrol/` (engine.py = scheduler, unifi.py = API client, ical.py = calendar, web.py,
telegram_bot.py, db.py, static/index.html), `mock_unifi.py` (fake controller for tests), `run.py`.

## Commands
Run locally against the fake controller: `python3 mock_unifi.py 12445` then `python3 run.py config.json`
(config: unifi.host `http://127.0.0.1:12445`, token `test-token`).
Run tests: `python3 tests/test_all.py`
Lint: not set up yet (standards: black for Python, prettier for JS/HTML; see DRIFT below).

## Conventions to keep
- Code style: Python formatted with black; JS/CSS/HTML with prettier (see .claude/standards/STANDARDS.md)
- Version bump = (no VERSION file yet; add one when the first release is cut)
- Never commit `config.json` (holds the UniFi API token and dashboard password) or `data/`.
- Door actions must go through `Engine.door_action` so they are logged to the activity log.

## Where it runs
- fair-server (Ubuntu, LAN 192.168.1.160), folder /srv/fair-apps/door-control, port 8480.
  Runs as systemd service `door-control` (see `door-control.service`), not Docker yet.
- Deploy (run by the person deploying, see People in .claude/standards/STANDARDS.md), as one copy-paste block:
  ```bash
  cd /srv/fair-apps/door-control && git pull && sudo systemctl restart door-control
  ```
- Never delete `data/doorcontrol.db` (schedules and the activity log live there).

## Connections to other apps
None. Talks to the UniFi Access console on the LAN, Google Calendar (secret iCal feed, read-only) and Telegram.

## Known DRIFT from the standards (to fix in their own PRs)
- Runs under systemd, not Docker Compose with a /data volume.
- Dashboard uses a single shared password (HTTP Basic), not Google sign-in; being replaced.
- No CSP, no CSRF protection, inline `<script>`/`<style>` in `static/index.html`.
- Not black/prettier formatted; first commit went straight to `main` (empty repo bootstrap).

## App-specific rules
- A wrong lock/unlock can leave a building open or someone locked out: the scheduler must fail safe
  (retry, never guess) and must not touch a door someone changed by hand.
- Door names in calendar events match part of the UniFi door name; keep that behavior.

## Team workflow
- Standards: .claude/standards/ (synced from the dev-standards repo; don't edit here)
- New work: /feature. Small bugs: /fix. Full review: /audit-app.
- Never push to main or merge. Changes go through pull requests that Jake or Kolt approves.
  Deploys: see People in .claude/standards/STANDARDS.md.
