# Fairgrounds Door Control 1.0.0

A friendly front end for UniFi Access: one-tap lock/unlock, repeating and one-time schedules,
Google Calendar-driven unlocks, and a Telegram bot. Python standard library only, runs in Docker
on fair-server.

It works through UniFi's _temporary lock rules_: when a schedule starts the door gets `keep_unlock`;
when it ends the door goes back to its normal UniFi schedule (`reset`) or is locked (`keep_lock`).
It never edits your UniFi schedules or policies.

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
  person's email. Scheduler rows show the schedule name, Telegram rows show "telegram". The log keeps
  the newest 1000 rows; sign-ins count toward that.

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

## Settings

| Where | What |
|---|---|
| `.env` next to `compose.yaml` (git-ignored, mode 600) | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOWED_DOMAINS=hardincountyfair.org`, `GOOGLE_ALLOWED_EMAILS=` (leave empty), `PUBLIC_HOSTS=doors.jakemc.me`, `ALLOWED_HOSTS` (fair-network addresses; empty refuses them all), optional `TRUSTED_PROXIES` (see below). See `.env.example`. |
| `/data/config.json` in the `door_control_data` volume (mode 600) | UniFi host, API token and optional `cert_sha256`, Telegram bot token and allowed chat IDs, Google Calendar secret iCal URL, time zone, default unlock minutes. See `config.example.json`. |
| `/data/doorcontrol.db`, `/data/session-secret`, `/data/accounts.json` | Schedules, door state and activity log; session signing key; break-glass account. |

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
for `https://<console address>:12445`. When `cert_sha256` is not set, the app works as before and prints
one warning at startup.

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

Back up the database, config and sign-in files to a dated folder (safe while the app runs):

```bash
(set -e
BK=/srv/fair-apps/door-control-backups/$(date +%Y%m%d-%H%M%S)
mkdir -p -m 700 "$BK"
cd /srv/fair-apps/door-control
docker compose run --rm -T --no-deps --user 0 -v "$BK":/out app sh -c 'python -c "import sqlite3; s=sqlite3.connect(\"/data/doorcontrol.db\"); d=sqlite3.connect(\"/out/doorcontrol.db\"); s.backup(d); d.close()" && for f in config.json accounts.json session-secret; do if [ -e /data/$f ]; then cp /data/$f /out/; fi; done && chmod 600 /out/*'
ls -l "$BK")
```

Restore one of those folders (stops the app, copies the files back, starts it):

```bash
(set -e
cd /srv/fair-apps/door-control
ls /srv/fair-apps/door-control-backups
read -rp "Backup folder name to restore: " NAME
BK=/srv/fair-apps/door-control-backups/$NAME
test -f "$BK/doorcontrol.db"
docker compose stop
docker compose run --rm -T --no-deps --user 0 -v "$BK":/in:ro app sh -c 'for f in doorcontrol.db config.json accounts.json session-secret; do if [ -e /in/$f ]; then cp /in/$f /data/$f && chown 10001:10001 /data/$f && chmod 600 /data/$f; fi; done'
docker compose up -d
sleep 20
docker compose ps)
```

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
Prompts for the two Google values; nothing to edit by hand:

```bash
(set -e
cd /srv/fair-apps/door-control
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
In Google Calendar > the calendar's settings > Integrate calendar > **Secret address in iCal format**.
Put it in `calendar.ical_url` in `/data/config.json`. Then create events titled like:

- `Unlock: Event Center` - unlocks that door for the event's time
- `Unlock: Event Center, Main Gate` - several doors
- `Unlock: ALL` - every door

Door names only need to match part of the UniFi door name. Recurring events work. Use
`buffer_before_min` / `buffer_after_min` to unlock early or stay open after (e.g. rental setup/cleanup).
Anyone who can edit that calendar can unlock doors, so share it carefully.

### 6. Telegram (optional)
Create a **new** bot with @BotFather (don't reuse FairSync's token; two programs can't poll one bot).
Put its token in `telegram.token`, restart, message the bot: it replies with your chat ID.
Add that to `allowed_chat_ids`, restart. Try `/help`. The bot works from anywhere and does not use
the dashboard sign-in.

### Moving from the old systemd install (one time)
The full move, rollback and later-deploy blocks are in `CLAUDE.md` under "Where it runs". Order:
`.env` (step 3) -> deploy block (it also pins the UniFi certificate) -> break-glass account ->
Cloudflare row (step 4) -> trusted-proxy checks -> manual checks.

## Notes and limits
- Lock rules need a UniFi Access hub that supports them (API v1.24.6+). `Lock now` uses `lock_now`.
- "Unlock for…" is capped at 8 hours (UniFi custom rule); use a schedule or "Keep unlocked" for longer.
- If someone changes a door by hand during a schedule, the scheduler leaves their change alone at the end.
- If UniFi is unreachable the scheduler keeps retrying every 30 seconds and catches up on restart.

## Development
- Tests (commands for Docker are in `CLAUDE.md`): `python3 tests/test_all.py`,
  `python3 tests/test_google.py`, `node tests/test_ui.cjs`.
- Try it without touching real doors: `python3 mock_unifi.py 12445`, then a `config.json` with host
  `http://127.0.0.1:12445` and token `test-token`, and
  `PUBLIC_HOSTS=localhost:8480 ALLOWED_HOSTS=localhost:8480 TRUSTED_PROXIES=127.0.0.1/32 python3 run.py config.json`.
  Create a local admin with `python3 manage.py set-password admin` and sign in at http://localhost:8480
  (browsers accept the Secure cookie on localhost).
