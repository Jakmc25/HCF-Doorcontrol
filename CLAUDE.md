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
`PUBLIC_HOSTS=localhost:8480 ALLOWED_HOSTS=localhost:8480 TRUSTED_PROXIES=127.0.0.1/32 python3 run.py config.json`
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
  Compose project `door-control`, service `app`, port 8480 published on `127.0.0.1` only, network
  `door-control-net` (172.31.248.0/29), volume `door_control_data` at `/data`.
- The dashboard is reachable only through https://doors.jakemc.me (shared Cloudflare tunnel, which
  connects to `127.0.0.1:8480`; sign-in required) or on fair-server itself. Nothing on the fair network
  can reach it directly (Jake's decision: no LAN dashboard, Secure cookies). Other containers cannot
  reach the host loopback, so only cloudflared/fair-server can vouch for `CF-Connecting-IP`. Publishing
  on `0.0.0.0` again would reopen header spoofing through Docker's port forwarder (README).
  If the internet or tunnel is down: no dashboard; scheduler and Telegram keep running; use the UniFi
  Access app.
- `.env` (next to compose.yaml, mode 600): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `GOOGLE_ALLOWED_DOMAINS=hardincountyfair.org`, `GOOGLE_ALLOWED_EMAILS=` (empty),
  `PUBLIC_HOSTS=doors.jakemc.me`,
  `ALLOWED_HOSTS=192.168.1.160:8480,fair-server:8480,localhost:8480,127.0.0.1:8480` (empty = refuse every
  fair-network address), optional `TRUSTED_PROXIES` (default `172.31.248.1/32`, the gateway of the app's
  own network `door-control-net` 172.31.248.0/29 in compose.yaml, where cloudflared on fair-server
  appears; only that peer may vouch for `CF-Connecting-IP` and the public Host, so other fair-app
  containers cannot; wrong value = public sign-in refused, never open; README "Trusted proxies" has the
  checks and what to do if the subnet clashes). README step 3 has the block that writes `.env`.
- `config.json` may set `unifi.cert_sha256` (console certificate fingerprint; README "Pin the UniFi
  console certificate"). Unset: not checked, one warning at startup.
- Deploy (run by the person deploying, see People in .claude/standards/STANDARDS.md), after a PR merges:
  ```bash
  (set -e; cd /srv/fair-apps/door-control && git checkout main && git pull --ff-only && docker compose up -d --build && docker compose ps && sleep 20 && if docker compose logs --tail=100 | grep -q "cert_sha256 is not set"; then echo "Next: pin the UniFi certificate with the block in README, 'Pin the UniFi console certificate'."; fi)
  ```
  The deploy prints a reminder when the UniFi certificate is not pinned yet; then run the README block
  "Pin the UniFi console certificate" (it reads the console address from config.json). Run it again
  after the console's certificate is renewed.
- Docker Engine on fair-server must be version 28 or newer (older versions could let other machines on
  the same network reach ports published on 127.0.0.1). Check before every deploy:
  ```bash
  docker version --format '{{.Server.Version}}'
  ```
  If it prints a version older than 28: do not deploy; tell Jake.
- Don't deploy within 15 minutes of a schedule or calendar window starting/ending, or during the fair.
- Never run `docker compose down -v`. Never run the old `door-control` systemd service and the container
  together (both drive the doors and poll the bot).
- Break-glass account: `docker compose exec app python manage.py set-password admin` (as the app user,
  never root).

### First move from systemd to Docker (1.0.0, one time)
Order: README step 2 (Google client) and step 3 (`.env`) -> this block (it also pins the UniFi console
certificate: it reads the console address from `unifi.host` in config.json, prints the fingerprint and
saves it as `unifi.cert_sha256`; nothing to type in) -> break-glass account -> README step 4
(Cloudflare row) -> README "Trusted proxies" checks -> manual checks. Pull and build happen while the old service still runs;
the old dashboard page may not load during the build, the scheduler keeps working. Once the old service
is stopped, every failure (a failed command, the old service still running, or a container that is not
healthy within 90 seconds) runs `restore_old`: stop the container, check out the old commit and start the
old service again.
```bash
(set -eE
cd /srv/fair-apps/door-control
test -f .env || { echo "Stop: create .env first (README step 3)."; exit 1; }
mkdir -p /srv/fair-apps/door-control-backups && chmod 700 /srv/fair-apps/door-control-backups
test -e /srv/fair-apps/door-control-backups/pre-docker-commit || git rev-parse HEAD > /srv/fair-apps/door-control-backups/pre-docker-commit
git checkout main
git pull --ff-only
docker compose build
CONSOLE=$(python3 -c 'import json,sys,urllib.parse; u=urllib.parse.urlsplit(json.load(open(sys.argv[1]))["unifi"]["host"]); h=u.hostname; h="[%s]" % h if ":" in h else h; print("%s:%s" % (h, u.port or 443))' config.json)
FP=$(openssl s_client -connect "$CONSOLE" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
if [ -z "$FP" ]; then echo "Stop: could not read the UniFi console certificate from $CONSOLE (nothing changed)."; exit 1; fi
echo "UniFi console $CONSOLE certificate SHA-256: $FP"
BK=/srv/fair-apps/door-control-backups/$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$BK"
restore_old() {
  trap - ERR
  echo "Deploy FAILED after the old service stopped: putting the old version back."
  docker compose stop || true
  git checkout -q "$(cat /srv/fair-apps/door-control-backups/pre-docker-commit)" || true
  sudo systemctl enable --now door-control || true
  systemctl is-active door-control || true
  echo "Old version restored. Backup in $BK"
}
trap restore_old ERR
sudo systemctl stop door-control
sudo systemctl disable door-control
if systemctl is-active --quiet door-control; then echo "Stop: the old door-control service is still running."; restore_old; exit 1; fi
umask 077
python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' data/doorcontrol.db "$BK/doorcontrol.db"
cp -p config.json "$BK"/
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); c.pop("web",None); c.pop("db_path",None); c["unifi"]["cert_sha256"]=sys.argv[3]; json.dump(c,open(sys.argv[2],"w"),indent=2)' "$BK/config.json" "$BK/config.docker.json" "$FP"
docker compose run --rm -T --no-deps --user 0 -v "$BK":/src:ro app sh -c 'if [ -e /data/doorcontrol.db ]; then echo "The volume already has a database; not overwriting it."; exit 1; fi; cp /src/doorcontrol.db /data/doorcontrol.db && cp /src/config.docker.json /data/config.json && chown 10001:10001 /data /data/doorcontrol.db /data/config.json && chmod 600 /data/config.json /data/doorcontrol.db'
docker compose up -d
STATUS=starting
for i in $(seq 1 18); do sleep 5; STATUS=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)"); if [ "$STATUS" = healthy ]; then break; fi; done
if [ "$STATUS" != healthy ]; then docker compose logs --tail=40; echo "Stop: the container is $STATUS, not healthy."; restore_old; exit 1; fi
trap - ERR
docker compose ps
docker compose logs --tail=40
echo "Backup in $BK")
```
The database is copied with SQLite's backup (safe if it has `-wal`/`-shm` files). Then create the
break-glass account (above), then the Cloudflare row (README step 4). Until that row exists nobody can
use the dashboard; that is expected.

Manual checks after the Cloudflare row: https://doors.jakemc.me shows the sign-in page; Google sign-in
with a hardincountyfair.org account reaches the dashboard and the email shows in the header; a gmail.com
account is refused; the door list loads; "Unlock 1 hr" on a test door shows your email in Recent
activity, then "Back to normal"; Telegram `/doors` answers; `http://192.168.1.160:8480` cannot be reached from
another fair-network computer (README "Trusted proxies"); `curl -s -H 'Host: other.jakemc.me' -H 'CF-Connecting-IP: 1.2.3.4' http://127.0.0.1:8480/api/doors`
returns 404; admin sign-in works at doors.jakemc.me; awards.jakemc.me still loads.

### Rollback to the systemd version
First remove the `doors.jakemc.me` row from the Cloudflare tunnel (that row only), then. Every check
runs before the container is stopped; after the stop, any failure starts the Docker version again
(`restore_new`) so the app is never left down:
```bash
(set -eE
cd /srv/fair-apps/door-control
OLD=$(cat /srv/fair-apps/door-control-backups/pre-docker-commit 2>/dev/null || true)
if [ -z "$OLD" ] || ! git rev-parse --verify -q "$OLD^{commit}" >/dev/null; then echo "Stop: no usable pre-docker-commit file; nothing was changed."; exit 1; fi
mkdir -p data
restore_new() {
  trap - ERR
  echo "Rollback FAILED: starting the Docker version again."
  git checkout -q main || true
  docker compose up -d || true
  docker compose ps || true
}
trap restore_new ERR
docker compose stop
if [ -e data/doorcontrol.db ]; then mv data/doorcontrol.db "data/doorcontrol.db.before-rollback-$(date +%Y%m%d-%H%M%S)"; fi
docker compose run --rm -T --no-deps --user 0 -v "$PWD/data":/out app sh -c 'python -c "import sqlite3; s=sqlite3.connect(\"/data/doorcontrol.db\"); d=sqlite3.connect(\"/out/doorcontrol.db\"); s.backup(d); d.close()" && chown --reference=/out /out/doorcontrol.db'
git checkout "$OLD"
trap - ERR
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
- 1.0.0 (2026-11-27, feature/google-signin, review rounds 1-3 fixed): Google sign-in (hardincountyfair.org) at doors.jakemc.me,
  break-glass admin password, CSRF, host lockdown, who-did-it activity log (additive `log.user`),
  Docker Compose, CSP + static split, black/prettier, VERSION/CHANGELOG. Review round 1: trusted
  proxies (round 2: own network door-control-net, gateway /32 only; round 3: port on 127.0.0.1 only), overall break-glass cap, request limits, UniFi certificate pinning, plain errors.

## Team workflow
- Standards: .claude/standards/ (synced from the dev-standards repo; don't edit here)
- New work: /feature. Small bugs: /fix. Full review: /audit-app.
- Never push to main or merge. Changes go through pull requests that Jake or Kolt approves.
  Deploys: see People in .claude/standards/STANDARDS.md.
