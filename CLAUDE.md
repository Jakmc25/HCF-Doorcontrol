# HCF Door Control

## What this app does
Friendly control panel for the fairgrounds' UniFi Access doors: one-tap lock/unlock, repeating and
one-time schedules, Google Calendar events ("Unlock: Event Center") that unlock doors, and a Telegram
bot. It uses UniFi's temporary lock rules (`keep_unlock`, `reset`, `keep_lock`, `lock_now`, `custom`)
through the local Access Open API (`https://<console>:12445`, bearer token). It never edits UniFi's own
schedules or policies. Every error, schedule change and door status change is also sent as a Telegram
alert (never delaying door control). Sign-in: Google for the admins in `GOOGLE_ALLOWED_EMAILS` and the
in-app People list (admins manage it), server-side 12-hour sessions. Current version: **1.2.0**.

## Stack
Language/runtime: Python 3.13 (Docker `python:3.13-slim-bookworm` + `tzdata`), standard library only
(http.server, sqlite3, urllib); plain JS front end, no build step.
Key libraries: none.
Layout: `doorcontrol/` (engine.py = scheduler, tick lock, watchdog, door matching, schedule changes;
unifi.py = API client; ical.py = calendar reader, acceptance rules, repeat rules, DST helpers; notify.py
= Telegram alert queue and sender thread; people.py = admin emails, People list helpers, email check,
calendar organizers; backup.py = daily backups; web.py = dashboard, sign-in gate, server-side
`Sessions`, `DoorGoogle`, People routes, CSRF and API; telegram_bot.py; db.py; app.py = startup
checks and wiring; google_signin.py + auth.py = shared sign-in modules copied from Awards; static/ =
index.html, app.js, style.css, login.html, login.js, fair-logo.png), `manage.py` (break-glass admin
accounts, `backup`, `sessions`, `revoke`), `mock_unifi.py` (fake controller for tests: `fail`, `fail_paths`, `hang`,
`set_by_hand`), `run.py`, `VERSION`.

## Commands
Run locally against the fake controller: `python3 mock_unifi.py 12445`, then
`DOORCONTROL_DEV_ALLOW_HTTP=1 PUBLIC_HOSTS=localhost:8480 ALLOWED_HOSTS=localhost:8480 TRUSTED_PROXIES=127.0.0.1/32 python3 run.py config.json`
(config: unifi.host `http://127.0.0.1:12445`, token `test-token`; data in `data/`; sign in with a local
`python3 manage.py set-password admin`). `DOORCONTROL_DEV_ALLOW_HTTP=1` allows plain http only to
127.0.0.1/localhost; without it the app refuses any console that is not https and pinned/verified.
Run tests (Docker, as on fair-server):
```bash
docker run --rm -v "$PWD":/app -w /app python:3.13-slim-bookworm sh -c 'apt-get -qq update && apt-get -qq install -y tzdata openssl >/dev/null && python tests/test_all.py && python tests/test_google.py && python tests/test_safety.py && python tests/test_alerts.py && python tests/test_qa.py'
docker run --rm -v "$PWD":/w -w /w node:22-alpine node tests/test_ui.cjs
```
Without Docker: `python3 tests/test_all.py && python3 tests/test_google.py && python3 tests/test_safety.py && python3 tests/test_alerts.py && python3 tests/test_qa.py && node tests/test_ui.cjs`.
Tests use only `mock_unifi.py`, a fake Telegram (`tests/support.py` `Recorder`) and fake clocks.
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
- Door actions must go through `Engine.door_action` (pass `user=` from the web) so they are logged and
  alerted. Schedule changes go through `Engine.add_schedule/update_schedule/delete_schedule(..., user=)`.
- `engine.py`, `telegram_bot.py`, `ical.py`, `unifi.py`, `db.py`, `notify.py`, `people.py`, `backup.py`
  never import `web`, `auth` or `google_signin`: the scheduler, bot, alerts and backups must keep working
  with nobody signed in.
- Alerts only through `Notifier.alert(kind, text, key)` (or `outage`/`recovered`): O(1), no I/O, never
  raises; a separate thread sends. Never call Telegram from the scheduler, calendar or web path. Alert
  texts are fixed templates; never put tokens or the calendar URL in them (`scrub` is the backstop).
- Fail safe: on doubt keep the current state, log and alert; never touch a door changed by hand; compare
  window times in UTC only (`ical.wall_to_utc`, `gap_end`); only one tick at a time (`Engine.tick` /
  `request_tick`). A calendar problem must never stop schedules.
- Schema changes are additive (see `DB._migrate`).
- Sessions (1.2.0): only `web.Sessions` makes them (cookie `__Host-doors_sid`, 12 h, `SESSION_HOURS`);
  the database keeps only `sha256(id)`. Every request re-checks the person (Google: admin, People list
  or allowed domain; break-glass: same account `gen`). Never use the shared modules' stateless
  `issue/verify/cookie_header` again.
- People list (1.2.0): admins = Google sessions whose email is in `GOOGLE_ALLOWED_EMAILS`; the
  break-glass `admin:NAME` is not an admin for it. People routes call `require_admin()`; add/remove is
  logged (kind `people`) and alerted (kind `people`); removal deletes that person's sessions in the
  same transaction (`DB.person_remove`).

## Shared code
`doorcontrol/google_signin.py` and `doorcontrol/auth.py` are unchanged copies from Awards
(`Jakmc25/HCF_Awards` commit `c16643e13dea42e7b2b74422f3d2eb544103d0cf`), pinned by
`RepoTests.test_shared_modules_unchanged`:
- `google_signin.py` sha256 `8b022dad96710068c35b4a578f361b5cd96e56a6f5d6947416d296c7cc179da1`
- `auth.py` sha256 `cf094dcf22d6a44d796f69fe3ce13abba68bc3d63579a10a50c6d5cba3a75cda`

Never patch them here: copy a newer Awards version and bump both hashes (test and this list).
Budget and Thank You copies were not yet compared with Awards. App-specific rules live in `web.py`
(stricter than Awards: always-Secure cookies, no "open until configured", CSRF on logout,
loopback-only `/health`, every Google refusal logged, server-side sessions) and `manage.py`
(12-character minimum). `web.DoorGoogle` subclasses `google_signin.Google` without editing it: the
`emails` property's setter keeps the env admins, its getter adds the People list, so the parent's
`finish()`, `ready()`, `start()` and `allowed()` work unchanged.
`WhoSignsInTests.test_door_google_relies_on_parent_behaviour` pins what the subclass relies on.
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
  `GOOGLE_ALLOWED_DOMAINS=` (empty since 1.2.0: no domain-wide access),
  `GOOGLE_ALLOWED_EMAILS=jake@hardincountyfair.org,kolt@hardincountyfair.org` (the admins),
  `PUBLIC_HOSTS=doors.jakemc.me`,
  `ALLOWED_HOSTS=192.168.1.160:8480,fair-server:8480,localhost:8480,127.0.0.1:8480` (empty = refuse every
  fair-network address), optional `TRUSTED_PROXIES` (default `172.31.248.1/32`, the gateway of the app's
  own network `door-control-net` 172.31.248.0/29 in compose.yaml, where cloudflared on fair-server
  appears; only that peer may vouch for `CF-Connecting-IP` and the public Host, so other fair-app
  containers cannot; wrong value = public sign-in refused, never open; README "Trusted proxies" has the
  checks and what to do if the subnet clashes). README step 3 has the block that writes `.env`.
- `config.json` must set `unifi.cert_sha256` (console certificate fingerprint; README "Pin the UniFi
  console certificate") unless `verify_tls` is true, and `unifi.host` must be https: since 1.1.0 the app
  otherwise refuses to start and sends nothing. The deploy block below pins it when missing. Run the
  README pin block again after the console's certificate is renewed.
- Deploy (run by the person deploying, see People in .claude/standards/STANDARDS.md) after a PR merges:
  the block in "Deploy" below (precondition: 1.0.0 or later already runs in Docker; if
  cert_sha256 is not set it pins the certificate before the restart).
- Daily backups: `/data/backups` inside the volume (14 kept, mode 600; README "Backup and restore").
  Deploy and rollback backups: `/srv/fair-apps/door-control-backups` (folders `deploy-*`,
  `pre-rollback-*`, mode 700; the newest 10 of each are kept).
- Docker Engine on fair-server must be version 28 or newer (older versions could let other machines on
  the same network reach ports published on 127.0.0.1). The deploy block checks it; to check by hand:
  ```bash
  docker version --format '{{.Server.Version}}'
  ```
  If it prints a version older than 28: do not deploy; tell Jake.
- Don't deploy within 15 minutes of a schedule or calendar window starting/ending, or during the fair.
- Never run `docker compose down -v`. Never run the old `door-control` systemd service and the container
  together (both drive the doors and poll the bot).
- Break-glass account: `docker compose exec app python manage.py set-password admin` (as the app user,
  never root).

### Upgrade to 1.2.0 (one time, before the Deploy block)
1.2.0 lets only the admins in `GOOGLE_ALLOWED_EMAILS` and the in-app People list sign in. This block
saves the current `.env` to `/srv/fair-apps/door-control-backups/env-<time>` (mode 600), keeps every
other line, sets `GOOGLE_ALLOWED_DOMAINS=` (empty) and
`GOOGLE_ALLOWED_EMAILS=jake@hardincountyfair.org,kolt@hardincountyfair.org`, and writes `.env` with
mode 600. Nothing to edit by hand. It changes nothing in the running app until the Deploy block
recreates the container:
```bash
(set -e
cd /srv/fair-apps/door-control
test -f .env || { echo "Stop: no .env here. Nothing changed."; exit 1; }
umask 077
mkdir -p -m 700 /srv/fair-apps/door-control-backups
ENVBK=/srv/fair-apps/door-control-backups/env-$(date +%Y%m%d-%H%M%S)
cp -p .env "$ENVBK"
chmod 600 "$ENVBK"
{ grep -v -e '^GOOGLE_ALLOWED_DOMAINS=' -e '^GOOGLE_ALLOWED_EMAILS=' .env || true; } > .env.new
grep -q '^GOOGLE_CLIENT_ID=' .env.new || { rm -f .env.new; echo "Stop: .env has no GOOGLE_CLIENT_ID line. Nothing changed."; exit 1; }
printf '%s\n' 'GOOGLE_ALLOWED_DOMAINS=' 'GOOGLE_ALLOWED_EMAILS=jake@hardincountyfair.org,kolt@hardincountyfair.org' >> .env.new
chmod 600 .env.new
mv .env.new .env
ls -l .env
grep '^GOOGLE_ALLOWED_' .env
echo "Old .env saved as $ENVBK. Now run the Deploy block below.")
```
Then run the Deploy block. After it, **everyone signs in again once** (Jake, Kolt and the break-glass
account): the old 14-day cookies stop working. Jake then adds other staff in Settings › People (bottom
of the dashboard). To undo the `.env` change, copy the saved `env-<time>` file back to `.env` and run
`docker compose up -d`.

### Deploy
In order: checks Docker >= 28 and that the old systemd service is not running (if the app is not
running it says so and goes on: that is the recovery case); records the previous commit and backs up the database, config.json, accounts.json and session-secret
to a new `deploy-<time>` folder (nothing has changed yet if any of this fails); pins the UniFi
certificate if it is not pinned (before any restart, so the new version never refuses to start); pulls
and builds while the old version keeps running; starts the new version and waits up to 90 s for
healthy. Any failure after the start puts the previous commit (and its image) back, starts it and
waits up to 90 s for it to be healthy. At the end only the newest 10 `deploy-*` and 10 `pre-rollback-*`
folders are kept.
```bash
(set -eE
cd /srv/fair-apps/door-control
V=$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)
if ! [ "${V%%.*}" -ge 28 ] 2>/dev/null; then echo "Stop: Docker Engine ${V:-unknown} is older than 28 (or not reachable). Nothing changed. Tell Jake."; exit 1; fi
if systemctl is-active --quiet door-control; then echo "Stop: the old systemd service is running; use the first-move block. Nothing changed."; exit 1; fi
if [ -z "$(docker compose ps --status running -q app)" ]; then echo "Note: Door Control is not running here; deploying anyway to bring it back."; fi
umask 077
BKROOT=/srv/fair-apps/door-control-backups
mkdir -p -m 700 "$BKROOT"
BK=$BKROOT/deploy-$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$BK"
PREV=$(git rev-parse HEAD)
echo "$PREV" > "$BK/previous-commit"
docker compose run --rm -T --no-deps --user 0 -e OWNER="$(id -u):$(id -g)" -v "$BK":/out app sh -c 'python -c "import sqlite3; s=sqlite3.connect(\"/data/doorcontrol.db\"); d=sqlite3.connect(\"/out/doorcontrol.db\"); s.backup(d); d.close()" && for f in config.json accounts.json session-secret; do if [ -e /data/$f ]; then cp /data/$f /out/; fi; done && chmod 600 /out/* && chown "$OWNER" /out/*'
test -f "$BK/doorcontrol.db" && test -f "$BK/config.json"
STATE=$(python3 -c 'import json,sys; u=json.load(open(sys.argv[1]))["unifi"]; print("http" if not str(u.get("host","")).startswith("https://") else "pinned" if u.get("cert_sha256") else "verified" if u.get("verify_tls") is True else "unpinned")' "$BK/config.json")
if [ "$STATE" = http ]; then echo "Stop: unifi.host in config.json is not https://. Nothing changed. Backup in $BK"; exit 1; fi
if [ "$STATE" = unpinned ]; then
CONSOLE=$(python3 -c 'import json,sys,urllib.parse; u=urllib.parse.urlsplit(json.load(open(sys.argv[1]))["unifi"]["host"]); h=u.hostname; h="[%s]" % h if ":" in h else h; print("%s:%s" % (h, u.port or 443))' "$BK/config.json")
FP=$(openssl s_client -connect "$CONSOLE" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
if [ -z "$FP" ]; then echo "Stop: could not read the UniFi console certificate from $CONSOLE. Nothing changed."; exit 1; fi
echo "UniFi console $CONSOLE certificate SHA-256: $FP"
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); c["unifi"]["cert_sha256"]=sys.argv[3]; json.dump(c,open(sys.argv[2],"w"),indent=2)' "$BK/config.json" "$BK/config.pinned.json" "$FP"
docker compose run --rm -T --no-deps app sh -c 'cat > /data/config.json.new && chmod 600 /data/config.json.new && mv /data/config.json.new /data/config.json' < "$BK/config.pinned.json"
fi
git checkout -q main
git pull --ff-only
docker compose build
restore_prev() {
  trap - ERR
  echo "Deploy FAILED: starting the previous version ($PREV) again."
  git checkout -q "$PREV" || true
  docker compose up -d || true
  R=starting
  for i in $(seq 1 18); do sleep 5; R=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)" 2>/dev/null || true); if [ "$R" = healthy ]; then break; fi; done
  docker compose ps || true
  if [ "$R" = healthy ]; then echo "The previous version is running and healthy."; else echo "The previous version is $R, not healthy: tell Jake. Use the UniFi Access app for urgent door changes."; fi
  echo "Backup in $BK"
}
trap restore_prev ERR
docker compose up -d
S=starting
for i in $(seq 1 18); do sleep 5; S=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)"); if [ "$S" = healthy ]; then break; fi; done
if [ "$S" != healthy ]; then docker compose logs --tail=40; echo "Stop: the new version is $S, not healthy."; restore_prev; exit 1; fi
trap - ERR
for P in deploy pre-rollback; do ls -1d "$BKROOT/$P"-* 2>/dev/null | sort | head -n -10 | xargs -r rm -rf --; done
docker compose ps
echo "Deployed $(cat VERSION). Backup in $BK")
```
Calendar (only if one is set up): opens doors only for events from `calendar.allowed_organizers` (or, if that key is missing, the admins in `GOOGLE_ALLOWED_EMAILS` plus the People list, since 1.2.0). With neither set, every "Unlock:" event is refused with a Telegram alert, nothing more. Today no calendar feed is configured, so nothing changes; when one is added, set `allowed_organizers` (README "Calendar").
After it: send the Telegram bot `/doors`; the next door change sends an alert to the allowed private
chats. Check `docker compose exec app python manage.py backup` prints a path.
Manual checks for 1.2.0: Jake signs in with Google and sees Settings › People; a hardincountyfair.org
account that is not on the list is refused (and shows in Recent activity); add a test person, sign in
as them (no People section), remove them with the typed confirmation and check they are signed out at
once and a Telegram alert arrived; `docker compose exec app python manage.py sessions` lists sessions.

### Rollback to the previous Docker version (1.1.0 and later)
Goes back to the commit and data of one `deploy-<time>` folder made by the deploy block (the typed
name must look like `deploy-20261201-093000`; anything else, such as `../`, stops it). Every check
runs before the container is stopped: the folder has the database, config.json and previous-commit; it
was not restored before (a `restored` marker makes a second run stop, so newer data is never
overwritten twice); the old systemd service is not running; and the running app has no activity or
schedule changes newer than the backup, unless you type `REPLACE NEWER DATA`. The current data is saved
to `pre-rollback-<time>` first. Then it stops the container, puts back doorcontrol.db, config.json,
accounts.json and session-secret, checks out the previous commit and waits for healthy; any failure
after the stop puts the current version and its data back (`restore_new`).
```bash
(set -eE
cd /srv/fair-apps/door-control
BKROOT=/srv/fair-apps/door-control-backups
ls -d "$BKROOT"/deploy-* 2>/dev/null || { echo "Stop: there are no deploy backups. Nothing changed."; exit 1; }
read -rp "Deploy backup to go back to (for example deploy-20261201-093000): " NAME
[[ "$NAME" =~ ^deploy-[0-9-]+$ ]] || { echo "Stop: type a folder name like deploy-20261201-093000. Nothing changed."; exit 1; }
BK=$BKROOT/$NAME
for f in doorcontrol.db config.json previous-commit; do test -f "$BK/$f" || { echo "Stop: $BK/$f is missing. Nothing changed."; exit 1; }; done
if [ -e "$BK/restored" ]; then echo "Stop: $NAME was already restored ($(cat "$BK/restored")); again would overwrite newer data. Nothing changed."; exit 1; fi
PREV=$(cat "$BK/previous-commit")
git rev-parse --verify -q "$PREV^{commit}" >/dev/null || { echo "Stop: commit $PREV is not known here. Nothing changed."; exit 1; }
if systemctl is-active --quiet door-control; then echo "Stop: the old systemd service is running. Nothing changed."; exit 1; fi
NEWER=$(docker compose run --rm -T --no-deps --user 0 -v "$BK":/bk:ro app python -c 'import sqlite3; q=lambda u: sqlite3.connect(u, uri=True).execute("SELECT COALESCE(MAX(ts),0),(SELECT COUNT(*) FROM schedules),(SELECT COALESCE(MAX(id),0) FROM schedules) FROM log").fetchone(); a=q("file:/data/doorcontrol.db?mode=ro"); b=q("file:/bk/doorcontrol.db?mode=ro&immutable=1"); print("newer" if a[0] > b[0] or a[1:] != b[1:] else "same")')
if [ "$NEWER" != same ]; then
  echo "Door Control has activity or schedule changes newer than $NAME. Going back replaces them (they are saved in a pre-rollback folder first)."
  read -rp "Type REPLACE NEWER DATA to continue: " OK
  if [ "$OK" != "REPLACE NEWER DATA" ]; then echo "Stop: nothing changed."; exit 1; fi
fi
umask 077
CUR=$(git rev-parse HEAD)
PRE=$BKROOT/pre-rollback-$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$PRE"
echo "$CUR" > "$PRE/previous-commit"
docker compose run --rm -T --no-deps --user 0 -e OWNER="$(id -u):$(id -g)" -v "$PRE":/out app sh -c 'python -c "import sqlite3; s=sqlite3.connect(\"/data/doorcontrol.db\"); d=sqlite3.connect(\"/out/doorcontrol.db\"); s.backup(d); d.close()" && for f in config.json accounts.json session-secret; do if [ -e /data/$f ]; then cp /data/$f /out/; fi; done && chmod 600 /out/* && chown "$OWNER" /out/*'
test -f "$PRE/doorcontrol.db" && test -f "$PRE/config.json"
echo "Current state saved in $PRE"
put_back() { docker compose run --rm -T --no-deps --user 0 -v "$1":/in:ro app sh -c 'for f in doorcontrol.db config.json accounts.json session-secret; do if [ -e /in/$f ]; then cp /in/$f /data/$f && chown 10001:10001 /data/$f && chmod 600 /data/$f; fi; done'; }
restore_new() {
  trap - ERR
  echo "Rollback FAILED: putting the current version ($CUR) and its data back."
  docker compose stop || true
  git checkout -q "$CUR" || true
  put_back "$PRE" || true
  docker compose up -d || true
  docker compose ps || true
}
trap restore_new ERR
docker compose stop
put_back "$BK"
git checkout -q "$PREV"
docker compose up -d
S=starting
for i in $(seq 1 18); do sleep 5; S=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)"); if [ "$S" = healthy ]; then break; fi; done
if [ "$S" != healthy ]; then docker compose logs --tail=40; echo "Stop: the previous version is $S, not healthy."; restore_new; exit 1; fi
trap - ERR
date > "$BK/restored"
for P in deploy pre-rollback; do ls -1d "$BKROOT/$P"-* 2>/dev/null | sort | head -n -10 | xargs -r rm -rf --; done
docker compose ps
echo "Back on $(cat VERSION) with the data of $NAME. The data from before is in $PRE")
```
If your connection drops after the stop, run `cd /srv/fair-apps/door-control && docker compose up -d`.
The repository is then on a detached commit; the next deploy block checks out `main` again. Both
versions run on the same database (migrations are additive).

### First move from systemd to Docker (1.0.0, one time)
This block uses `sudo` (stop/disable the old service), so it can stop at a **sudo password prompt**
halfway through. Run `sudo -v` right before pasting it so sudo is already unlocked.
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
Only for going back from the first move. It copies the database only: the systemd version reads its own
`config.json` in the repository folder (it still has the old `web` password), not `/data/config.json`.
It uses `sudo` too; run `sudo -v` first.
First remove the `doors.jakemc.me` row from the Cloudflare tunnel (that row only), then run this block. Every check
runs before the container is stopped; after the stop, any failure (including the old service not
starting) starts the Docker version again (`restore_new`) so the app is never left down:
```bash
(set -eE
cd /srv/fair-apps/door-control
OLD=$(cat /srv/fair-apps/door-control-backups/pre-docker-commit 2>/dev/null || true)
if [ -z "$OLD" ] || ! git rev-parse --verify -q "$OLD^{commit}" >/dev/null; then echo "Stop: no usable pre-docker-commit file; nothing was changed."; exit 1; fi
mkdir -p data
restore_new() {
  trap - ERR
  echo "Rollback FAILED: starting the Docker version again."
  sudo systemctl stop door-control || true
  sudo systemctl disable door-control || true
  git checkout -q main || true
  docker compose up -d || true
  docker compose ps || true
}
trap restore_new ERR
docker compose stop
if [ -e data/doorcontrol.db ]; then mv data/doorcontrol.db "data/doorcontrol.db.before-rollback-$(date +%Y%m%d-%H%M%S)"; fi
docker compose run --rm -T --no-deps --user 0 -v "$PWD/data":/out app sh -c 'python -c "import sqlite3; s=sqlite3.connect(\"/data/doorcontrol.db\"); d=sqlite3.connect(\"/out/doorcontrol.db\"); s.backup(d); d.close()" && chown --reference=/out /out/doorcontrol.db'
git checkout "$OLD"
sudo systemctl enable --now door-control
sleep 5
systemctl is-active --quiet door-control
trap - ERR
systemctl status door-control --no-pager)
```
The old version reads the copied database (the `user` column is additive). The volume stays. If your
connection drops while the block runs (after the stop), run `cd /srv/fair-apps/door-control && docker compose up -d`
to bring the Docker version back (or `sudo systemctl enable --now door-control` for the old one), never both.

To go forward again, first stop and disable the old service (two copies must never run together),
then run the normal deploy block:
```bash
(set -e; sudo systemctl stop door-control; sudo systemctl disable door-control; ! systemctl is-active --quiet door-control)
```

## Connections to other apps
No app-to-app links; no app reads this app's data. Talks to the UniFi Access console on the LAN (:12445),
Google Calendar (secret iCal feed, read-only), Telegram (own bot token), Google OAuth (sign-in, client
in Jake's Google Cloud project "HCF Door Control") and the shared Cloudflare tunnel (doors.jakemc.me).

## Known DRIFT from the standards
None open in this app (O1/R11, no automatic backups, was fixed in 1.1.0). Follow-up elsewhere (O8/R12,
AC51): dev-standards APPS.md needs a Door Control row and a "Shared code" mention, in a separate
dev-standards PR after 1.1.0 deploys.

## App-specific rules
- A wrong lock/unlock can leave a building open or someone locked out: the scheduler must fail safe
  (retry, never guess) and must not touch a door someone changed by hand.
- Calendar door names (1.1.0, `engine.match_doors`): a whole door name is used as is; otherwise each
  part must be an exact name or a partial match of exactly one door (Jake's rule); none/several = that
  part opens nothing and is alerted. "ALL" / "*" never open anything from the calendar.
- Calendar events open doors only when CONFIRMED, from an allowed organizer, not an unanswered
  invitation, within `max_window_hours`; feed unknown/stale = no new calendar window, open ones end at
  their recorded calendar end (`cal_ends_at`), never earlier, even when an overlapping schedule ends
  first. Back-to-back calendar windows never hold a door over `max_window_hours` (<= 24) without a break.
- Deploy blocks build before they stop the running service.

## History
- 2026-11 first version: scheduler, web dashboard (shared HTTP Basic password), Telegram bot, Google
  Calendar sync; systemd service.
- 1.0.0 (2026-11-27, feature/google-signin, review rounds 1-3 fixed): Google sign-in (hardincountyfair.org) at doors.jakemc.me,
  break-glass admin password, CSRF, host lockdown, who-did-it activity log (additive `log.user`),
  Docker Compose, CSP + static split, black/prettier, VERSION/CHANGELOG. Review round 1: trusted
  proxies (round 2: own network door-control-net, gateway /32 only; round 3: port on 127.0.0.1 only), overall break-glass cap, request limits, UniFi certificate pinning, plain errors.
- 1.2.0 (2026-11-28, feature/people-sessions, PR 2 of the plan in docs/features/safety-alerts, AC40,
  AC41, AC41a): server-side sessions (only sha256 of the id stored, 12 h, cookie `__Host-doors_sid`,
  re-checked at each request, logout deletes the row, `manage.py sessions`/`revoke`, set-password and
  remove-user end that account's sessions; old `doors_auth` cookies refused and cleared); Google only
  for admins (`GOOGLE_ALLOWED_EMAILS`) and the in-app People list (`web.DoorGoogle`, shared modules
  unchanged), refusals logged; Settings › People for admins (add with email check, remove with typed
  confirmation, admins not removable, removal signs out at once, logged + Telegram alert); calendar
  organizers default to admins + People. Additive tables `sessions`, `people`; log kind `people`.
  `.env` values change (one-time block above); no new `.env` variables; everyone signs in again once.
- 1.1.0 (2026-11-28, feature/safety-alerts, PR 1 of the plan in docs/features/safety-alerts; audit
  docs/audits/2026-11-27.md): calendar safety (status, organizer allowlist, invitations, no ALL, exact
  matching, validated/bounded RRULEs with nth/last weekday, EXDATE by day, feed checks, unknown/stale,
  24 h cap), UTC + DST rules, tick lock, end action at end, per-door isolation, hand-change alerts,
  outage logging, watchdog + `/health` 503, Telegram alerts (notify.py), Telegram offset/age/rate
  limit/attribution, startup refusal without https + pin, daily backups + `manage.py backup`, log by
  age, Docker log rotation, TZ, new deploy and rollback blocks. Additive schema: `door_state.source/
  ends_at/hand_alerted`, `log.kind`, `kv`. No new `.env` lines. Server sessions and the in-app people
  list (AC40/41/41a) come in PR 2 (1.2.0).
  Review round 1 fixes (2026-11-28): a calendar hold is kept in `door_state.cal_ends_at` (additive) and
  never shortened while the calendar is stale or not read yet, also when a schedule overlaps (R1); an
  empty/unclear lock rule is never a hand change, it is retried and alerted (Q1), read failures log one
  row per outage (Q2); back-to-back calendar windows may not hold a door over 24 h, `max_window_hours`
  capped at 24 (S11), all-day buffers not counted (R3); alert for every calendar window that opens a
  door, with title and UID (S12); no `allowed_organizers` in config.example.json (R2); links stripped
  from titles in alerts (S15); strangers checked before the Telegram limit (S16); startup alert for
  `DOORCONTROL_DEV_ALLOW_HTTP` (S17); BOM accepted (Q3); deploy runs when the app is down and
  `restore_prev` waits for healthy (R5), newest 10 deploy/rollback folders kept (S13), rollback name
  checked (S14); docs for R4/Q5 and Q4.

## Team workflow
- Standards: .claude/standards/ (synced from the dev-standards repo; don't edit here)
- New work: /feature. Small bugs: /fix. Full review: /audit-app.
- Never push to main or merge. Changes go through pull requests that Jake or Kolt approves.
  Deploys: see People in .claude/standards/STANDARDS.md.
