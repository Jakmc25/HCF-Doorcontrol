# HCF Door Control

## What this app does
Friendly control panel for the fairgrounds' UniFi Access doors: one-tap lock/unlock, repeating and
one-time schedules, Google Calendar events ("Unlock: Event Center") that unlock doors, and a Telegram
bot. It uses UniFi's temporary lock rules (`keep_unlock`, `reset`, `keep_lock`, `lock_now`, `custom`)
through the local Access Open API (`https://<console>:12445`, bearer token). It never edits UniFi's own
schedules or policies. Current version: **1.0.0**.

## Stack
Language/runtime: Python 3.13 (Docker `python:3.13-slim-bookworm` + `tzdata`), standard library only
(http.server, sqlite3, urllib); plain JS front end, no build step.
Key libraries: none.
Layout: `doorcontrol/` (engine.py = scheduler, unifi.py = API client, ical.py = calendar, web.py =
dashboard, sign-in gate, CSRF and API, telegram_bot.py, db.py, google_signin.py + auth.py = shared
sign-in modules copied from Awards, static/ = index.html, app.js, style.css, login.html, login.js,
fair-logo.png), `manage.py` (break-glass admin accounts), `mock_unifi.py` (fake controller for tests),
`run.py`, `VERSION`.

## Commands
Run locally against the fake controller: `python3 mock_unifi.py 12445`, then
`PUBLIC_HOSTS=localhost:8480 ALLOWED_HOSTS=localhost:8480 python3 run.py config.json`
(config: unifi.host `http://127.0.0.1:12445`, token `test-token`; data in `data/`; sign in with a local
`python3 manage.py set-password admin`).
Run tests (Docker, as on fair-server):
```bash
docker run --rm -v "$PWD":/app -w /app python:3.13-slim-bookworm sh -c 'apt-get -qq update && apt-get -qq install -y tzdata >/dev/null && python tests/test_all.py && python tests/test_google.py'
docker run --rm -v "$PWD":/w -w /w node:22-alpine node tests/test_ui.cjs
```
Without Docker: `python3 tests/test_all.py && python3 tests/test_google.py && node tests/test_ui.cjs`.
Lint:
```bash
docker run --rm -v "$PWD":/w -w /w python:3.13-slim-bookworm sh -c 'pip install -q black && black --check .'
docker run --rm -v "$PWD":/w -w /w node:22-alpine npx --yes prettier@3 --check "doorcontrol/static/*.{html,js,css}" "tests/*.cjs"
```

## Conventions to keep
- Code style: Python formatted with black; JS/CSS/HTML with prettier (`.prettierrc.json`, HTML strict
  whitespace; see .claude/standards/STANDARDS.md). Don't format `fair-logo.png` or the shared modules by hand.
- Version bump = `VERSION` + `image: door-control:X.Y.Z` in compose.yaml + README title + a new
  `CHANGELOG.md` entry + "Current version" above.
- New top-level files must be added to the Dockerfile `COPY` (today: `run.py manage.py VERSION` and the
  whole `doorcontrol/` folder).
- Strict CSP (`script-src 'self'; style-src 'self'`): no inline `<script>`, `<style>`, `style=""` or
  `on...=""` in HTML or JS-built HTML strings.
- Every route goes in `web.ROUTES` (needs a session; non-GET also needs the CSRF token). Only the sign-in
  page, `LOGIN_STATIC`, `SIGNIN_ROUTES` and loopback `/health` are open.
- Never commit `.env`, `config.json` (UniFi/Telegram tokens, calendar URL) or `data/`.
- Door actions must go through `Engine.door_action` (pass `user=` from the web) so they are logged.
- `engine.py`, `telegram_bot.py`, `ical.py`, `unifi.py`, `db.py` never import `web`, `auth` or
  `google_signin`: the scheduler and bot must keep working with nobody signed in.
- Schema changes are additive (see `DB._migrate`).

## Shared code
`doorcontrol/google_signin.py` and `doorcontrol/auth.py` are unchanged copies from Awards
(`Jakmc25/HCF_Awards` commit `c16643e13dea42e7b2b74422f3d2eb544103d0cf`), pinned by
`RepoTests.test_shared_modules_unchanged`:
- `google_signin.py` sha256 `8b022dad96710068c35b4a578f361b5cd96e56a6f5d6947416d296c7cc179da1`
- `auth.py` sha256 `cf094dcf22d6a44d796f69fe3ce13abba68bc3d63579a10a50c6d5cba3a75cda`

Never patch them here: copy a newer Awards version and bump both hashes (test and this list).
Budget and Thank You copies were not yet compared with Awards. App-specific rules live in `web.py`
(stricter than Awards: always-Secure cookies, no "open until configured", CSRF on logout,
loopback-only `/health`, every Google refusal logged) and `manage.py` (12-character minimum).
`login.html`, `login.js` and `fair-logo.png` are adapted copies of Awards' files.

## Where it runs
- fair-server (Ubuntu, LAN 192.168.1.160), folder `/srv/fair-apps/door-control` (git checkout), Docker
  Compose project `door-control`, service `app`, port 8480, volume `door_control_data` at `/data`.
- Public: https://doors.jakemc.me through the shared Cloudflare tunnel (sign-in required).
  `http://192.168.1.160:8480` shows the sign-in page only (cookies are always `Secure`; no LAN sign-in).
  If the internet or tunnel is down: no dashboard; scheduler and Telegram keep running; use the UniFi
  Access app.
- `.env` (next to compose.yaml, mode 600): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `GOOGLE_ALLOWED_DOMAINS=hardincountyfair.org`, `GOOGLE_ALLOWED_EMAILS=` (empty),
  `PUBLIC_HOSTS=doors.jakemc.me`,
  `ALLOWED_HOSTS=192.168.1.160:8480,fair-server:8480,localhost:8480,127.0.0.1:8480` (empty = refuse every
  fair-network address). README step 3 has the block that writes it.
- Deploy (run by the person deploying, see People in .claude/standards/STANDARDS.md), after a PR merges:
  ```bash
  (set -e; cd /srv/fair-apps/door-control && git checkout main && git pull --ff-only && docker compose up -d --build && docker compose ps)
  ```
- Don't deploy within 15 minutes of a schedule or calendar window starting/ending, or during the fair.
- Never run `docker compose down -v`. Never run the old `door-control` systemd service and the container
  together (both drive the doors and poll the bot).
- Break-glass account: `docker compose exec app python manage.py set-password admin` (as the app user,
  never root).

### First move from systemd to Docker (1.0.0, one time)
Order: README step 2 (Google client) and step 3 (`.env`) -> this block -> break-glass account ->
README step 4 (Cloudflare row) -> manual checks. Pull and build happen while the old service still runs;
the old dashboard page may not load during the build, the scheduler keeps working. If anything fails
after the old service stops, the block stops the container and starts the old service again.
```bash
(set -e
cd /srv/fair-apps/door-control
test -f .env || { echo "Stop: create .env first (README step 3)."; exit 1; }
mkdir -p /srv/fair-apps/door-control-backups && chmod 700 /srv/fair-apps/door-control-backups
test -e /srv/fair-apps/door-control-backups/pre-docker-commit || git rev-parse HEAD > /srv/fair-apps/door-control-backups/pre-docker-commit
git checkout main
git pull --ff-only
docker compose build
BK=/srv/fair-apps/door-control-backups/$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$BK"
trap 'echo "Deploy FAILED after the old service stopped: putting the old version back."; docker compose stop || true; git checkout -q "$(cat /srv/fair-apps/door-control-backups/pre-docker-commit)"; sudo systemctl enable --now door-control; systemctl is-active door-control' ERR
sudo systemctl stop door-control
sudo systemctl disable door-control
if systemctl is-active --quiet door-control; then echo "Stop: the old door-control service is still running."; exit 1; fi
cp -p data/doorcontrol.db config.json "$BK"/
umask 077
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); c.pop("web",None); c.pop("db_path",None); json.dump(c,open(sys.argv[2],"w"),indent=2)' "$BK/config.json" "$BK/config.docker.json"
docker compose create
docker run --rm --user 0 -v door_control_data:/data -v "$BK":/src:ro door-control:1.0.0 sh -c 'if [ -e /data/doorcontrol.db ]; then echo "The volume already has a database; not overwriting it."; exit 1; fi; cp /src/doorcontrol.db /data/doorcontrol.db && cp /src/config.docker.json /data/config.json && chown 10001:10001 /data /data/doorcontrol.db /data/config.json && chmod 600 /data/config.json /data/doorcontrol.db'
docker compose up -d
trap - ERR
sleep 30
docker compose ps
docker compose logs --tail=40
echo "Backup in $BK")
```
`docker compose ps` must show `healthy`. Then create the break-glass account (above), then the
Cloudflare row (README step 4). Until that row exists nobody can use the dashboard; that is expected.

Manual checks after the Cloudflare row: https://doors.jakemc.me shows the sign-in page; Google sign-in
with a hardincountyfair.org account reaches the dashboard and the email shows in the header; a gmail.com
account is refused; the door list loads; "Unlock 1 hr" on a test door shows your email in Recent
activity, then "Back to normal"; Telegram `/doors` answers; `http://192.168.1.160:8480` shows only the
sign-in page; `curl -s -H 'Host: other.jakemc.me' -H 'CF-Connecting-IP: 1.2.3.4' http://127.0.0.1:8480/api/doors`
returns 404; admin sign-in works at doors.jakemc.me; awards.jakemc.me still loads.

### Rollback to the systemd version
First remove the `doors.jakemc.me` row from the Cloudflare tunnel (that row only), then:
```bash
(set -e
cd /srv/fair-apps/door-control
docker compose down
mv data/doorcontrol.db "data/doorcontrol.db.before-rollback-$(date +%Y%m%d-%H%M%S)"
docker run --rm --user 0 -v door_control_data:/data:ro -v "$PWD/data":/out door-control:1.0.0 sh -c 'cp /data/doorcontrol.db /out/doorcontrol.db && chown --reference=/out /out/doorcontrol.db'
git checkout "$(cat /srv/fair-apps/door-control-backups/pre-docker-commit)"
sudo systemctl enable --now door-control
sleep 5
systemctl status door-control --no-pager)
```
The old version reads the copied database (the `user` column is additive). The volume stays. To go
forward again: the normal deploy block.

## Connections to other apps
No app-to-app links; no app reads this app's data. Talks to the UniFi Access console on the LAN (:12445),
Google Calendar (secret iCal feed, read-only), Telegram (own bot token), Google OAuth (sign-in, client
in Jake's Google Cloud project "HCF Door Control") and the shared Cloudflare tunnel (doors.jakemc.me).

## Known DRIFT from the standards
None open in this app. Follow-ups elsewhere: dev-standards APPS.md needs a Door Control row and a
"Shared code" mention (after deploy).

## App-specific rules
- A wrong lock/unlock can leave a building open or someone locked out: the scheduler must fail safe
  (retry, never guess) and must not touch a door someone changed by hand.
- Door names in calendar events match part of the UniFi door name; keep that behavior.
- Deploy blocks build before they stop the running service.

## History
- 2026-11 first version: scheduler, web dashboard (shared HTTP Basic password), Telegram bot, Google
  Calendar sync; systemd service.
- 1.0.0 (2026-11-27, feature/google-signin): Google sign-in (hardincountyfair.org) at doors.jakemc.me,
  break-glass admin password, CSRF, host lockdown, who-did-it activity log (additive `log.user`),
  Docker Compose, CSP + static split, black/prettier, VERSION/CHANGELOG.

## Team workflow
- Standards: .claude/standards/ (synced from the dev-standards repo; don't edit here)
- New work: /feature. Small bugs: /fix. Full review: /audit-app.
- Never push to main or merge. Changes go through pull requests that Jake or Kolt approves.
  Deploys: see People in .claude/standards/STANDARDS.md.
