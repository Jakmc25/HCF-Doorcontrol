# Fairgrounds Door Control 1.1.0

A friendly front end for UniFi Access: one-tap lock/unlock, repeating and one-time schedules,
Google Calendar-driven unlocks, and a Telegram bot. Python standard library only, runs in Docker
on fair-server.

It works through UniFi's _temporary lock rules_: when a schedule starts the door gets `keep_unlock`;
when it ends the door goes back to its normal UniFi schedule (`reset`) or is locked (`keep_lock`).
It never edits your UniFi schedules or policies.

Fail-safe rule everywhere: when Door Control is unsure it keeps the current state, logs it and sends a
Telegram alert. A door changed by hand in UniFi is never touched again for that window.

## Signing in

Open **https://doors.jakemc.me** from anywhere and press **Sign in with Google**. Any verified
`@hardincountyfair.org` Google account can view and control every door and schedule. Personal
accounts (gmail.com and others) are refused. Your email shows at the top right, next to **Sign out**.

- The dashboard is reachable only through **https://doors.jakemc.me** (or on fair-server itself).
  Nothing on the fair network can connect to it directly: the port is published on fair-server's
  loopback only, and sign-in needs HTTPS anyway. The scheduler and the Telegram bot are not affected.
- A sign-in lasts 14 days (`SESSION_DAYS` in `doorcontrol/web.py`). **Sign out** clears it in that
  browser only.
- **Sign everyone out** (for example after a lost laptop): delete the session secret and restart.
  Everyone, including admin sign-ins, has to sign in again:
  ```bash
  (cd /srv/fair-apps/door-control && docker compose exec app rm -f /data/session-secret && docker compose restart && docker compose ps)
  ```
- Every door action, schedule change and sign-in is written to **Recent activity** with the
  person's email. Scheduler rows show the schedule name, calendar rows `calendar:<organizer>`, Telegram
  rows `telegram:<chat id>/<name>`. Rows are kept for 90 days (`log_days` in config.json); sign-ins
  are also capped at the newest 5000, so they never push door actions out.

### Docker Engine version on fair-server

Docker Engine must be version **28 or newer**: older versions could let other machines on the same
network reach ports published on `127.0.0.1`. Check before every deploy:

```bash
docker version --format '{{.Server.Version}}'
```

If it prints a version older than 28: do not deploy; tell Jake.

### If the internet or the Cloudflare tunnel is down

There is no dashboard (it is reachable only through doors.jakemc.me). The scheduler and the Telegram
bot keep running on fair-server. Use the **UniFi Access app** on the fair network to lock or
unlock a door by hand.

## If you cannot sign in

1. **Use the admin sign-in.** On the sign-in page at doors.jakemc.me, open **Admin sign-in** (small
   link under the Google button) and use the break-glass account. It only appears once an account
   exists (see below).
2. **Check the Google settings** in `/srv/fair-apps/door-control/.env` (client ID and secret, see
   "Google OAuth client" below), then restart:
   ```bash
   (cd /srv/fair-apps/door-control && docker compose up -d && docker compose logs --tail=30)
   ```
   The log says "Google sign-in is not set up" when the values are missing, and prints every refused
   sign-in with the reason.
3. **Last resort:** the UniFi Access app. The scheduler and Telegram bot do not need anyone signed in.

### Break-glass admin account

One password account for when Google sign-in is broken. Use a **long random password** (20+
characters, from a password manager); the command refuses anything under 12 characters. It is stored
only as a scrypt hash in `/data/accounts.json`. Ten wrong tries from one address lock that address
out for 15 minutes. There is no way to create or change it from the web.

Always run these through `docker compose exec app` (the app's own user), never as root, or the app
cannot read the file:

```bash
(cd /srv/fair-apps/door-control && docker compose exec app python manage.py set-password admin && docker compose exec app python manage.py users)
```

Remove it: `(cd /srv/fair-apps/door-control && docker compose exec app python manage.py remove-user admin)`.
Setting a new password signs that account out everywhere.

## Telegram alerts

When a Telegram bot token and allowed chat IDs are set (step 6 below), every allowed **private** chat
gets a message for:

- **Errors:** a door action that fails (from the dashboard or Telegram at once; from the scheduler
  after 2 failed tries), the UniFi console unreachable, a scheduler error or stall, a calendar feed
  that cannot be read, a calendar event that is refused (and why), the calendar going stale, a failed
  backup. Each says what happened and what Door Control does about it.
- **Schedule changes:** added, changed, paused, resumed or deleted, with the email of who did it.
- **Door status changes:** every lock, unlock and back-to-normal from any source (person, Telegram,
  schedule, calendar), with the end time when known, and doors changed by hand in UniFi.
- **Recovered:** one message when an outage ends (UniFi, calendar, scheduler, backups).

Alerts never slow down or block door control: they wait in a small queue and a separate thread sends
them. The same alert is sent once, then at most every 30 minutes with "repeated N times since ...";
at most 20 alerts a minute (the rest arrive as one summary). The limit counts alerts, not messages:
with 2 allowed chats that is up to 40 Telegram messages a minute (Jake's choice). Alerts never contain tokens or the
calendar address. If the bot token or the chat list is empty, alerts are only written to the server
log. Switch a kind off in `config.json` under `alerts.kinds` (`door_error`, `scheduler`, `calendar`,
`backup`, `schedule_change`, `status`, `hand_change`, `recovered`; `people` is used from 1.2.0); the other `alerts` settings are
`repeat_minutes`, `max_per_minute`, `queue_size` and `failures_before_alert`.

## Settings

| Where | What |
|---|---|
| `.env` next to `compose.yaml` (git-ignored, mode 600) | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOWED_DOMAINS=hardincountyfair.org`, `GOOGLE_ALLOWED_EMAILS=` (leave empty), `PUBLIC_HOSTS=doors.jakemc.me`, `ALLOWED_HOSTS` (defense in depth: host names accepted for direct requests; the port is published on loopback only, so it matters only if the port is ever re-published on the fair network; empty refuses every other address), optional `TRUSTED_PROXIES` (see below). See `.env.example`. |
| `/data/config.json` in the `door_control_data` volume (mode 600) | UniFi host (must be `https://`), API token and `cert_sha256` (required), Telegram bot token, allowed chat IDs and limits, Google Calendar secret iCal URL and calendar rules, `alerts`, time zone, default unlock minutes, `hand_check_minutes` (5), `log_days` (90). See `config.example.json`. |
| `/data/doorcontrol.db`, `/data/session-secret`, `/data/accounts.json` | Schedules, door state and activity log; session signing key; break-glass account. |
| `/data/backups` | Daily backups (see "Backup and restore"). |

### Logs

`docker compose logs` holds sign-in emails and IP addresses (and door names and alert texts), so treat
it like the activity log. Docker keeps at most 5 files of 10 MB each (`logging:` in `compose.yaml`).
Log times are in local time (`TZ=America/New_York` in the image).

### Health check

`/health` answers only on the container's own loopback (Docker's health check): `{"ok": true,
"version": ...}`, or HTTP 503 with `"ok": false` when no scheduler check finished in the last 90
seconds or the scheduler stopped. Docker then shows the container as **unhealthy** and a Telegram alert
"Scheduler stalled" is sent once (and "running again" when it recovers).

Changing `GOOGLE_ALLOWED_DOMAINS` or `GOOGLE_ALLOWED_EMAILS` and restarting signs out every Google
session made under the old rule.

### Trusted proxies (TRUSTED_PROXIES in .env)

Door Control believes the `CF-Connecting-IP` header and the doors.jakemc.me hostname only when the
connection comes from a trusted proxy: `cloudflared` (the Cloudflare tunnel) on fair-server. Anyone else
who sends those headers is treated as an ordinary fair-network visitor.

The port is published on fair-server's loopback only (`127.0.0.1:8480` in `compose.yaml`), so the only
way in is from fair-server itself, where `cloudflared` runs. Other containers (the other fair apps)
cannot reach the host's loopback, and fair-network computers cannot connect at all. The app runs on its
own small Docker network, `door-control-net` (`172.31.248.0/29`); a connection to `127.0.0.1:8480` on
fair-server arrives from that network's gateway, `172.31.248.1`, which is the default
`TRUSTED_PROXIES`. If the value is wrong, sign-in at doors.jakemc.me is refused with "Use the fair server
address"; the app never opens up. The Host and `TRUSTED_PROXIES` checks stay in the app as a second
layer.

**Confirm the address `cloudflared` appears from** (on fair-server; prints the gateway, then a sign-in
check made the same way `cloudflared` connects, which must say `"remote": true`):

```bash
(cd /srv/fair-apps/door-control && docker network inspect door-control-net -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' && curl -s -H 'Host: doors.jakemc.me' http://127.0.0.1:8480/api/login-info; echo)
```

If `cloudflared` runs in its own Docker container instead of on fair-server itself, it does not come
from that gateway: ask before changing `TRUSTED_PROXIES`.

**Check that a fair-network computer cannot connect.** Run this on another computer on the fair network
(Mac or Linux terminal; on Windows use `curl.exe` in PowerShell), not on fair-server. It must fail with
"Connection refused" or a timeout (curl prints `000`), never a number like `200` or `403`:

```bash
curl -s -m 5 -o /dev/null -w "%{http_code}\n" -H 'Host: doors.jakemc.me' -H 'CF-Connecting-IP: 1.2.3.4' http://192.168.1.160:8480/api/login-info
```

**If `docker compose up` says the subnet overlaps** another network, list the networks and their
subnets on fair-server:

```bash
(docker network ls && docker network inspect $(docker network ls -q) -f '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}')
```

Pick a free `/29` (for example `172.31.249.0/29`), then change it in `compose.yaml` (subnet and
gateway) and `TRUSTED_PROXIES` in `.env` (`172.31.249.1/32`) in the same pull request, and deploy.

**Publishing on the fair network (not recommended).** Changing the one port line in `compose.yaml` from
`"127.0.0.1:8480:8480"` to `"0.0.0.0:8480:8480"` would show the sign-in page at
`http://192.168.1.160:8480` again (sign-in itself still only works at doors.jakemc.me). Warning: Docker's
port forwarder can then make other containers and host traffic arrive from the same gateway address as
`cloudflared`, so they could fake `CF-Connecting-IP` and the public Host again. Only do this together
with a secret header that the tunnel adds and the app requires (not built yet).

### Pin the UniFi console certificate (unifi.cert_sha256)

The console uses a self-signed certificate. Pinning it means nobody on the LAN can pretend to be the
console and catch the API token. Do this once after the move to Docker (the first-move block in
`CLAUDE.md` already does it) and again whenever the console's certificate is renewed (the dashboard
then says the certificate "does not match"). The block reads the console address from `unifi.host` in
`/data/config.json`, so there is nothing to type in:

```bash
(set -e
cd /srv/fair-apps/door-control
umask 077
docker compose run --rm -T --no-deps app cat /data/config.json > /tmp/door-config.json
CONSOLE=$(python3 -c 'import json,sys,urllib.parse; u=urllib.parse.urlsplit(json.load(open(sys.argv[1]))["unifi"]["host"]); h=u.hostname; h="[%s]" % h if ":" in h else h; print("%s:%s" % (h, u.port or 443))' /tmp/door-config.json)
FP=$(openssl s_client -connect "$CONSOLE" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
if [ -z "$FP" ]; then echo "Stop: could not read the certificate from $CONSOLE."; rm -f /tmp/door-config.json; exit 1; fi
echo "UniFi console $CONSOLE certificate SHA-256: $FP"
python3 -c 'import json,sys; p=sys.argv[1]; c=json.load(open(p)); c["unifi"]["cert_sha256"]=sys.argv[2]; json.dump(c,open(p,"w"),indent=2)' /tmp/door-config.json "$FP"
docker compose run --rm -T --no-deps app sh -c 'cat > /data/config.json.new && chmod 600 /data/config.json.new && mv /data/config.json.new /data/config.json' < /tmp/door-config.json
rm /tmp/door-config.json
docker compose restart
sleep 20
docker compose ps
docker compose logs --tail=30)
```

It prints the fingerprint it saved. To double-check it, compare with the certificate your browser shows
for `https://<console address>:12445`.

**Since 1.1.0 Door Control refuses to start** (and sends nothing to the console) when `unifi.host` is
not `https://`, or when `cert_sha256` is not set and `verify_tls` is not `true`. The normal deploy
block in `CLAUDE.md` pins the certificate automatically before the restart when it is missing.

### Change config.json

Opens the file in an editor, checks it is valid JSON, puts it back and restarts:

```bash
(set -e
cd /srv/fair-apps/door-control
umask 077
docker compose run --rm -T --no-deps app cat /data/config.json > /tmp/door-config.json
nano /tmp/door-config.json
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' /tmp/door-config.json
docker compose run --rm -T --no-deps app sh -c 'cat > /data/config.json.new && chmod 600 /data/config.json.new && mv /data/config.json.new /data/config.json' < /tmp/door-config.json
rm /tmp/door-config.json
docker compose restart
docker compose ps)
```

## Backup and restore

**Automatic:** once a day the app writes `/data/backups/door-control-YYYYmmdd-HHMMSS.tar.gz` holding
the database (a safe SQLite copy), `config.json` (UniFi and Telegram tokens, calendar address),
`accounts.json` and `session-secret`. The 14 newest are kept; the files are readable only by the app
(mode 600, folder 700) because they hold tokens. A failed backup sends a Telegram alert. Make one now
and see the list:

```bash
(cd /srv/fair-apps/door-control && docker compose exec app python manage.py backup && docker compose exec app ls -l /data/backups)
```

These stay inside the volume. To restore one of them, extract it into a dated folder and use the
restore block below with that folder:

```bash
(set -e
cd /srv/fair-apps/door-control
docker compose exec app ls /data/backups
read -rp "Backup file name to unpack: " FILE
BK=/srv/fair-apps/door-control-backups/unpacked-$(date +%Y%m%d-%H%M%S)
mkdir -p -m 700 "$BK"
docker compose exec -T app cat "/data/backups/$FILE" | tar -xzf - -C "$BK"
ls -l "$BK"
echo "Now restore folder: $(basename "$BK")")
```

**By hand:** back up the database, config and sign-in files to a dated folder (safe while the app runs):

```bash
(set -e
BK=/srv/fair-apps/door-control-backups/$(date +%Y%m%d-%H%M%S)
mkdir -p -m 700 "$BK"
cd /srv/fair-apps/door-control
docker compose run --rm -T --no-deps --user 0 -v "$BK":/out app sh -c 'python -c "import sqlite3; s=sqlite3.connect(\"/data/doorcontrol.db\"); d=sqlite3.connect(\"/out/doorcontrol.db\"); s.backup(d); d.close()" && for f in config.json accounts.json session-secret; do if [ -e /data/$f ]; then cp /data/$f /out/; fi; done && chmod 600 /out/*'
ls -l "$BK")
```

Restore one of those folders. First it saves the current state to a new dated `pre-restore-` folder
(same layout as a backup), then stops the app, copies the files back and starts it. If the safety
backup fails, nothing is changed:

```bash
(set -e
cd /srv/fair-apps/door-control
ls /srv/fair-apps/door-control-backups
read -rp "Backup folder name to restore: " NAME
BK=/srv/fair-apps/door-control-backups/$NAME
test -f "$BK/doorcontrol.db"
PRE=/srv/fair-apps/door-control-backups/pre-restore-$(date +%Y%m%d-%H%M%S)
mkdir -p -m 700 "$PRE"
docker compose run --rm -T --no-deps --user 0 -v "$PRE":/out app sh -c 'python -c "import sqlite3; s=sqlite3.connect(\"/data/doorcontrol.db\"); d=sqlite3.connect(\"/out/doorcontrol.db\"); s.backup(d); d.close()" && for f in config.json accounts.json session-secret; do if [ -e /data/$f ]; then cp /data/$f /out/; fi; done && chmod 600 /out/*'
test -f "$PRE/doorcontrol.db"
echo "Current state saved in $PRE"
trap 'echo "Restore FAILED: starting the app again with the data as it was."; docker compose up -d || true' ERR
docker compose stop
docker compose run --rm -T --no-deps --user 0 -v "$BK":/in:ro app sh -c 'for f in doorcontrol.db config.json accounts.json session-secret; do if [ -e /in/$f ]; then cp /in/$f /data/$f && chown 10001:10001 /data/$f && chmod 600 /data/$f; fi; done'
trap - ERR
docker compose up -d
sleep 20
docker compose ps)
```

If your connection drops while that block runs (after the stop), run `cd /srv/fair-apps/door-control && docker compose up -d`.

Never run `docker compose down -v`: it deletes the volume with every schedule and the activity log.

## Setting it up

### 1. UniFi Access API token
1. Open UniFi (Applications > **Access**) > Settings > Security > **Advanced** > API Token.
2. **Create New**, name it "Door Control", pick a long validity, and give it the **space** permissions
   (view + edit: doors and lock rules).
3. Copy the token into `unifi.token` in `/data/config.json` (see "Change config.json"). The console's
   API is `https://<console ip>:12445`.

### 2. Google OAuth client (Jake, in a browser)
1. https://console.cloud.google.com -> project picker -> **New project** -> `HCF Door Control` -> Create, then select it.
2. **APIs & Services -> OAuth consent screen** (Google Auth Platform): app name `Fairgrounds Door Control`,
   support email = yours, **Audience: External**, scopes only `openid`, `email`, `profile`, then
   **Publish app** (status "In production"; these scopes need no Google review).
3. **Clients -> Create client -> Web application**, name `Door Control`, **Authorized redirect URI**
   exactly `https://doors.jakemc.me/api/auth/google/callback`. Keep the Client ID and secret open.
4. **IAM & Admin -> Grant access** -> Kolt's Google account, role **Owner**, so the client does not
   depend on one person.

To replace a lost or deleted client, repeat steps 2-3 and write the new values with the `.env` block below.

### 3. `.env` on fair-server
Prompts for the two Google values; nothing to edit by hand. The block **writes a whole new `.env`**
(it replaces everything in it) and therefore refuses to run when a `.env` already exists. To change
one value later, edit that line in `.env` with `nano` and run `docker compose up -d`:

```bash
(set -e
cd /srv/fair-apps/door-control
if [ -e .env ]; then echo "Stop: .env already exists; this block would replace all of it. Nothing was changed."; exit 1; fi
read -rp "Google Client ID: " GID
read -rsp "Google Client secret (hidden): " GSECRET; echo
umask 077
cat > .env <<EOF
GOOGLE_CLIENT_ID=$GID
GOOGLE_CLIENT_SECRET=$GSECRET
GOOGLE_ALLOWED_DOMAINS=hardincountyfair.org
GOOGLE_ALLOWED_EMAILS=
PUBLIC_HOSTS=doors.jakemc.me
ALLOWED_HOSTS=192.168.1.160:8480,fair-server:8480,localhost:8480,127.0.0.1:8480
EOF
ls -l .env)
```

If the app is already running, apply it with `(cd /srv/fair-apps/door-control && docker compose up -d)`.

### 4. Cloudflare public hostname (only after the container is healthy and the admin account exists)
Cloudflare Zero Trust -> Networks -> Tunnels -> the fair-server tunnel -> Configure -> **Public Hostname**.

1. **Check first:** note the existing rows (take a screenshot). Make sure there is no `doors.jakemc.me`
   row and no wildcard row (`*.jakemc.me`). If either exists, stop and ask before changing anything.
2. **Add a public hostname:** Subdomain `doors`, Domain `jakemc.me`, Service type copied from the
   awards.jakemc.me row (`HTTP`), URL **`127.0.0.1:8480`** (write `127.0.0.1`, not `localhost`: the
   port is published on IPv4 loopback only).
3. Save. Do not edit any other row; the catch-all rule must stay last.
4. Open awards.jakemc.me and one other app to confirm they still load.

Until this row exists the dashboard cannot be used by anyone; that is expected.

### 5. Google Calendar (optional)
No calendar is set up today. When one is added, use a **dedicated internal calendar** that only fair
staff add dates to, with **invitations off** (Google Calendar > Settings > Event settings > "Add
invitations to my calendar": only when I respond). In its settings > Integrate calendar, copy the
**Secret address in iCal format** into `calendar.ical_url` in `/data/config.json`, and the
calendar's own address ("Calendar ID", ends in `@group.calendar.google.com`) into both
`calendar.allowed_organizers` (a list, add the key yourself: `config.example.json` leaves it out on
purpose) and `calendar.calendar_email`. Then create events titled like:

- `Unlock: Event Center` - unlocks that door for the event's time
- `Unlock: Event Center, Main Gate` - several doors (split on commas, `&` and `and`)

Rules (each refused event is written to the server log and sent as a Telegram alert):
- Only events with status **confirmed** count; tentative and cancelled events never open a door.
- The organizer must be in `calendar.allowed_organizers`. If that key is missing, the emails in
  `GOOGLE_ALLOWED_EMAILS` count. **An empty list (`"allowed_organizers": []`) still counts as set**: it
  means nobody, and the `GOOGLE_ALLOWED_EMAILS` fallback is not used. Leave the key out to use the admins.
  If both are empty, no event opens a door (one alert at startup).
  Google lists a secondary calendar itself as the organizer, so activity shows `calendar:<that address>`.
- The organizer check only proves what the feed says. Google writes the organizer, but **the real
  control is who can add or edit events on that calendar**: anyone with "Make changes to events" on it
  can unlock doors. Keep that list to the people who should open doors. Every calendar event that opens
  a door is sent as a Telegram alert with its title and UID, so an unexpected one is seen at once.
- An invitation nobody accepted (`NEEDS-ACTION` or `TENTATIVE`; no answer counts as not accepted) never
  opens a door, even from an allowed organizer. With `calendar_email` set, only that attendee counts;
  without it, every attendee other than the organizer.
- Door names: if the whole text is a door's name it is used as is ("Pole Barn and Arena" works when that
  is a door). Otherwise each part must be a door's exact name, or match part of exactly one door's name.
  A part that matches no door or several doors opens nothing; the other parts still apply. `ALL` and
  `*` never open anything: list the doors.
- An event may be at most `max_window_hours` (24, and never more than 24 whatever `config.json` says)
  long, counting `buffer_before_min` / `buffer_after_min`. An **all-day event** unlocks from
  midnight to midnight local time and counts as 24 hours even on a daylight-saving day, buffers not counted; an
  all-day event spanning several days is refused (make one event per day).
- Events back to back (or overlapping) on the same door may not keep it unlocked without a break for
  more than `max_window_hours` plus the buffers. Every event in such a run that would end past that
  opens nothing on that door (alerted) until the calendar itself has a break. So a daily all-day
  repeating event unlocks the door only on its first day: use a schedule for doors that should stay
  open for days. Door Control looks back `max_window_hours / 24 + 1` days (rounded up) for events
  that are still running, so any allowed window is seen.
- Repeating events: daily, weekly (on chosen weekdays), monthly (on day numbers, or "2nd Tuesday" /
  "last Friday"), yearly, with an end date or count. Anything else (for example "5th Monday",
  hourly) is skipped and alerted, never guessed. Door Control tries at most 5,000 dates per repeating
  event, so a **daily** event that started more than about 13 years ago is skipped and alerted: end it
  and make a new one.
- Daylight saving: a start in the hour that does not exist when the clocks spring forward (2:00-2:59
  AM) is skipped that day and alerted; an end in that hour moves to 3:00 AM. In the repeated hour in
  November an event runs once (at the first 1:xx). Schedules follow the same rule.
- Feed problems: an empty, web-page or cut-off reply keeps the last calendar that was read, so no door
  closes early (a feed starting with a byte order mark is read normally). Until the first good read after a start, and when the feed has not been read for
  `stale_minutes` (60, the dashboard says "Calendar stale"), no calendar unlock starts; a door already
  open for an event closes at that event's end as it was known, also when a schedule holds the same
  door and ends sooner (the calendar's end is kept separately and never shortened while the calendar
  cannot be trusted).

Use `buffer_before_min` / `buffer_after_min` to unlock early or stay open after (e.g. rental
setup/cleanup). Anyone who can edit that calendar can unlock doors, so share it carefully.

### 6. Telegram (optional)
Create a **new** bot with @BotFather (don't reuse FairSync's token; two programs can't poll one bot).
Put its token in `telegram.token`, restart, message the bot: it replies with your chat ID.
Add that to `allowed_chat_ids`, restart. Try `/help`. The bot works from anywhere and does not use
the dashboard sign-in. The same chats get the alerts (see "Telegram alerts").

Safety: commands only work in private chats with the bot (groups and buttons are ignored); a message
older than 2 minutes (`max_age_seconds`), for example one sent while the app was down, is dropped
without action; a chat may send 10 commands a minute (`max_commands_per_minute`), the rest get one
"Slow down" reply. A chat that is not allowed gets at most one "Not authorized" reply a minute and is
never counted in that limit. Door actions show in Recent activity as `telegram:<chat id>/<name>`.

### Moving from the old systemd install (one time)
The full move, rollback and later-deploy blocks are in `CLAUDE.md` under "Where it runs". Order:
`.env` (step 3) -> deploy block (it also pins the UniFi certificate) -> break-glass account ->
Cloudflare row (step 4) -> trusted-proxy checks -> manual checks.

The first-move block uses `sudo` (to stop the old service), so it can stop at a **sudo password
prompt** halfway. Run `sudo -v` right before pasting it, so sudo is already unlocked.

## Notes and limits
- Lock rules need a UniFi Access hub that supports them (API v1.24.6+). `Lock now` uses `lock_now`.
- "Unlock for…" is capped at 8 hours (UniFi custom rule); use a schedule or "Keep unlocked" for longer.
- If someone changes a door by hand during a schedule or calendar window (found within 5 minutes, or at
  the end), the scheduler reports it once and leaves the door alone.
- If UniFi is unreachable the scheduler keeps retrying every 30 seconds and catches up on restart; the
  activity log gets one row per door and action per outage, and one when it works again. That includes
  reading the door at the end of a window. An empty or unclear answer from UniFi about a door is never
  taken as a change by hand: the door is left as it is, the read is retried every tick and alerted as
  "could not read the door".
- When two windows overlap on a door, what happens at the end ("back to normal" or "lock") is chosen
  when the last one ends, from the windows active just before.

## Development
- Tests (commands for Docker are in `CLAUDE.md`): `python3 tests/test_all.py`,
  `python3 tests/test_google.py`, `python3 tests/test_safety.py`, `python3 tests/test_alerts.py`,
  `node tests/test_ui.cjs`.
- Try it without touching real doors: `python3 mock_unifi.py 12445`, then a `config.json` with host
  `http://127.0.0.1:12445` and token `test-token`, and
  `DOORCONTROL_DEV_ALLOW_HTTP=1 PUBLIC_HOSTS=localhost:8480 ALLOWED_HOSTS=localhost:8480 TRUSTED_PROXIES=127.0.0.1/32 python3 run.py config.json`.
  `DOORCONTROL_DEV_ALLOW_HTTP=1` allows plain http only to `127.0.0.1` / `localhost` (the fake
  console); it is never set in `compose.yaml`.
  Create a local admin with `python3 manage.py set-password admin` and sign in at http://localhost:8480
  (browsers accept the Secure cookie on localhost).
