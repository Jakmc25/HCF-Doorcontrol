# Plan: Google sign-in for the dashboard (and bring Door Control in line with the standards)

Spec: `docs/features/google-signin/SPEC.md` (approved 2026-11-27). Branch: `feature/google-signin`. Release: **1.0.0**.
Reference (read-only, not modified): Awards `/home/claude/hcf_awards` (`google_signin.py`, `auth.py`, `server.py`, `manage.py`, `tests/test_google.py`, Dockerfile, compose.yaml).

## Approach

**Reuse first.** `google_signin.py` and `auth.py` are copied from Awards into `doorcontrol/` **unchanged** (byte-for-byte after black), so a future sign-in fix can be copied across as-is. Everything app-specific lives in `web.py` (the gate) and `manage.py` (CLI), modelled on Awards `server.py`/`manage.py`:

| Awards `server.py` piece | Door Control (`web.py`) |
|---|---|
| `send()` security headers (CSP `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, `X-Robots-Tag`, `Cache-Control: no-store`) | Same, on **every** response (401/403/404/429/500, health, static). No public/frameable pages. |
| `remote = host in PUBLIC_HOSTS or CF-Connecting-IP`; remote with other Host -> 404; non-remote Host not in `ALLOWED_HOSTS` -> 403 | Same. |
| `/api/login-info`, `/api/auth/google/start` + `CALLBACK` (remote only), `/api/login` (same-origin + 10 fails/15 min/address, 429) | Same routes and logic. `login-info` also returns `password: bool(accounts)` (AC32). `/api/login` refused when no account exists and on non-public hosts (cookie is `Secure`, see below). |
| Session cookie `awards_auth` = `AUTH.verify() or GOOGLE.verify()` | Cookie `doors_auth`, same verify order. |
| CSRF: `/api/bootstrap` sets signed `awards_session` cookie, token = `sign(cookie)` in `X-CSRF-Token`, plus `same_origin()` | Cookie `doors_session`, same signing; checked on every POST/PUT/DELETE, **including `/api/logout`** (Awards' logout has no CSRF; AC15 requires it). |
| Error mapping; 500 body is a plain sentence | Same; `DoorError` -> 404, `ValueError` -> 400, `UnifiError` -> 502 (message kept, as today), anything else -> 500 "The request could not be completed. Nothing was changed." + `log.exception` (AC7). |

**Deliberate differences from Awards** (stricter, required by the spec; listed for integration review):
1. No "fresh install stays open" branch. Awards lets the fair network in without sign-in while no sign-in method exists; here every route except the sign-in page, its 3 static files, the auth routes and loopback `/health` needs a session, always (AC1, AC13).
2. All auth cookies are always `Secure` (AC14, AC30). Consequence: the plain-HTTP LAN address (`http://192.168.1.160:8480`) only shows the sign-in page, whose Google button goes to `https://doors.jakemc.me/api/auth/google/start` (AC11); the password form is shown only on the public host.
3. `/health` answers only requests from `127.0.0.1`/`::1` without `CF-Connecting-IP` (Docker healthcheck), handled before the Host checks so it needs no `ALLOWED_HOSTS` entry (AC24). The existing `/api/health` (scheduler/calendar status) stays, behind sign-in.
4. Session lifetime in one place: `web.SESSION_DAYS = 14`; `make_server` sets `google_signin.DAYS = auth.DAYS = SESSION_DAYS` (both modules read `DAYS` at call time), so the copied modules stay identical to Awards (AC16).
5. Every Google refusal is written to the server log by `web.py` (`log.warning("google sign-in refused: %s (%s)", message, ip)`), not only the domain refusal the module prints (AC9).
6. Minimum break-glass password 12 characters, enforced in `manage.py` before `Auth.set_password` (module keeps Awards' 10) (AC31). No web route creates, changes or lists accounts.

**Scheduler and Telegram stay independent of the web layer.** `engine.py`, `telegram_bot.py`, `ical.py`, `unifi.py` do not import `web`, `auth` or `google_signin`. `app.main()` keeps its order: load config -> DB -> calendar.start -> engine.start -> Bot.start -> `make_server(...)`. Google not configured only disables the Google button (AC13). The only engine change is an optional `user=""` argument on `Engine.door_action`, passed through to `DB.log` (AC18, AC22). Schedule change log rows are written by `web.py` through `engine.db.log` after the DB call succeeds (AC19). Sign-in rows likewise (AC20).

**Activity log rows written by the web layer:**
| Event | door | action | source | detail | user |
|---|---|---|---|---|---|
| door action | door label | action | `web` | as today | email / account name |
| schedule add / edit / pause / resume / delete | `""` | `schedule add` etc. | `web` | `added schedule "Office hours"` etc. Pause/resume = a PUT where only `enabled` changed (compare with `db.get_schedule(sid)` before update). | email |
| Google sign-in | `""` | `sign-in` | `web` | `signed in` | email |
| break-glass sign-in | `""` | `sign-in` | `web` | `signed in (password)` | account name |

**Front end.** `index.html` becomes markup only; CSS -> `static/style.css`; JS -> `static/app.js`. `style="margin-top:12px"` becomes a class; `f-cancel` uses the `hidden` attribute instead of `style.display`. `app.js` first calls `/api/bootstrap` (CSRF token, `user`, `version`), sends `X-CSRF-Token` on every change, shows the email and a Sign out button in the header, and reloads to the sign-in page on any 401. "By" column shows `r.user || r.source`. New `login.html` + `login.js` (copied from Awards, adapted: title "Fairgrounds Door Control", "Admin sign-in" in a closed `<details>` shown only if `password` is true and the host is public). `fair-logo.png` copied from Awards `static/` because `google_signin.page()` references it.

**Config moves into Docker.**
| Setting | Where after this PR | Why |
|---|---|---|
| `timezone`, `unifi.{host,token,verify_tls}`, `telegram.{token,allowed_chat_ids}`, `calendar.*`, `default_unlock_minutes`, `tick_seconds` | `/data/config.json` in the volume, mode 600, owner uid 10001 | Unchanged app settings; the UniFi/Telegram tokens and secret iCal URL stay in one 600 file, not in the environment. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOWED_DOMAINS`, `GOOGLE_ALLOWED_EMAILS` (blank), `PUBLIC_HOSTS`, `ALLOWED_HOSTS` | `.env` next to `compose.yaml` (AC25) | Same as Awards; sign-in settings. |
| session secret, break-glass accounts, database | `/data/session-secret` (600), `/data/accounts.json` (600, via `auth.Auth.save`), `/data/doorcontrol.db` | AC24, AC31. |
| `web.password`, `web.bind`, `web.port`, `db_path` | removed | AC5. In Docker, `DATA_DIR=/data` decides config/db/secret paths; bind/port come from env `BIND` (default `0.0.0.0`) and `PORT` (default `8480`). If old keys are still present they are ignored with one warning line. | **Note (round 3): this is inside the container; the host publish is `127.0.0.1:8480` only.**

Local runs keep working: `python3 run.py config.json` uses `DATA_DIR` default `data/`.

## Commit order (one PR)
1. **`chore: format with black and prettier`** - black on all `.py`; add `.prettierrc.json` (`{"htmlWhitespaceSensitivity": "strict"}`, as Awards) and `prettier --write doorcontrol/static/index.html`. No logic change. Record `python3 tests/test_all.py` output before and after (same test count, all OK) in the PR description (AC26).
2. **`feat: Docker and CSP-ready static files`** - `Dockerfile`, `compose.yaml`, `.dockerignore`, `.env.example`, `VERSION` (1.0.0), `.gitignore` adds `.env`; split `index.html` into `index.html` + `app.js` + `style.css`; `web.py` serves `/static/<name>` (resolved path must stay inside `static/`, as Awards `static()`) and sends the security headers; `app.py` reads `DATA_DIR`/`BIND`/`PORT`; `tests/test_ui.cjs` added. Still Basic auth in this commit (never deployed on its own).
3. **`feat: Google sign-in, CSRF, host lockdown, break-glass, who-did-it log`** - copy `google_signin.py`, `auth.py`; rewrite the gate in `web.py`; `manage.py`; `login.html`/`login.js`/`fair-logo.png`; `db.py` migration + `user` arg; `engine.door_action(user=)`; remove `web.password` everywhere (`app.py` startup check, `config.example.json`); `tests/test_google.py`; `tests/test_all.py` `WebTests` switched from Basic auth to a test session (helper issues a cookie with the server's `GOOGLE.issue()` and fetches the CSRF token); `test_auth` replaced by "Basic header is not accepted". All other `test_all.py` tests unchanged.
4. **`docs: 1.0.0 README, CHANGELOG, CLAUDE.md`** - README rewritten, `CHANGELOG.md` created, CLAUDE.md updated, `door-control.service` kept with a header comment "rollback only".

## Files to create or change
| File | Change | Why |
|------|--------|-----|
| `doorcontrol/google_signin.py` | New, copy of Awards (unchanged) | Google OAuth flow, state/nonce/`hd` checks, policy hash (AC8-AC14) |
| `doorcontrol/auth.py` | New, copy of Awards (unchanged) | scrypt accounts, rate limit, password sessions (AC17, AC30-AC32) |
| `doorcontrol/web.py` | Rewrite gate; keep the existing route table and handlers | AC1-AC7, AC11, AC15, AC16, AC18-AC20, AC23 |
| `doorcontrol/db.py` | Additive migration: if `PRAGMA table_info(log)` lacks `user`, `ALTER TABLE log ADD COLUMN "user" TEXT NOT NULL DEFAULT ''`; `log(..., user="")` | AC18, AC21 |
| `doorcontrol/engine.py` | `door_action(..., source="manual", user="")` passes `user` to `db.log` | AC18, AC22 |
| `doorcontrol/app.py` | `DATA_DIR` paths, env `BIND`/`PORT`, drop `web.password` check, warn on obsolete keys, pass `data_dir` to `make_server` | AC5, AC24 |
| `doorcontrol/static/index.html` | Markup only, header user + Sign out, no inline script/style | AC15, AC23 |
| `doorcontrol/static/app.js`, `style.css` | New (moved from `index.html`) + bootstrap/CSRF/401/By column | AC4, AC15, AC21, AC23 |
| `doorcontrol/static/login.html`, `login.js`, `fair-logo.png` | New (adapted from Awards) | AC2, AC30, AC32 |
| `manage.py` | New: `set-password NAME` (getpass, or stdin when not a TTY; refuses < 12 chars), `users`, `remove-user NAME` | AC31 |
| `config.example.json` | Remove `web` section and `db_path` | AC5 |
| `Dockerfile` | New: `python:3.13-slim-bookworm`, `apt-get install tzdata` (engine needs `zoneinfo` data) and a build-time `ZoneInfo("America/New_York")` check, user `doors` uid 10001, `COPY run.py manage.py VERSION ./` + `COPY doorcontrol ./doorcontrol`, `ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1`, `EXPOSE 8480`, `HEALTHCHECK` on `http://127.0.0.1:8480/health` | AC24, AC27 |
| `compose.yaml` | New: `name: door-control`, `image: door-control:1.0.0`, `restart: unless-stopped`, `init: true`, `ports: "0.0.0.0:8480:8480"`, env from `.env` (`${VAR:-}`), volume `door_control_data:/data` with `name: door_control_data`, `read_only: true`, `tmpfs: /tmp:size=16m,mode=1777`, `no-new-privileges` | AC24, AC25 | **SUPERSEDED in round 3: the port is published as `127.0.0.1:8480:8480` (loopback only).**
| `.dockerignore` | New: `.git`, `.env`, `config.json`, `data/`, `*.db`, `tests`, `docs`, `.claude`, `__pycache__`, `mock_unifi.py` | AC25 |
| `.env.example` | New: the 6 keys, no real values | AC25 |
| `.gitignore` | Add `.env` | AC25 |
| `.prettierrc.json` | New, as Awards | AC26 |
| `VERSION`, `CHANGELOG.md` | New, 1.0.0 | AC27 |
| `tests/test_google.py` | New (unittest, same fake token endpoint technique as Awards) | AC29 |
| `tests/test_all.py` | `WebTests` use a session + CSRF; black formatting | AC5, AC29 |
| `tests/test_ui.cjs` | New Node test (pattern of Awards `tests/test_ui.cjs`: fake DOM + fetch in `vm`) | AC15, AC21, AC23 |
| `README.md` | Rewritten: sign-in, `.env`, OAuth/Cloudflare setup, break-glass (long random password), "sign everyone out" (delete `/data/session-secret`, restart), backup/restore, changing `config.json` in the volume, Tailscale/password text removed | AC16, AC28 |
| `CLAUDE.md` | Stack, commands, version-bump rule, Where it runs (Docker, doors.jakemc.me, deploy block), tests, Connections (Google OAuth, Cloudflare tunnel), History, DRIFT section reduced to "none open" | AC27, AC28 |
| `door-control.service` | Kept, comment "used only for rollback" | Rollback |

## Data / API / config / contract changes
- **DB:** one additive column `log.user TEXT NOT NULL DEFAULT ''`; no table recreate, no other schema change. The old version reads the migrated database (it inserts with explicit columns; `SELECT *` just returns an extra field). Log cap stays 1000 rows; sign-in rows share it.
- **API:** new `GET /api/login-info`, `GET /api/bootstrap`, `POST /api/login`, `POST /api/logout`, `GET /api/auth/google/start`, `GET /api/auth/google/callback`, `GET /health` (loopback only), `GET /static/<file>`. Existing routes unchanged in path/method/body; `/api/log` rows gain `user`. All change routes need `X-CSRF-Token`.
- **Config:** see the table under Approach. **No contract** in `.claude/standards/contracts/` is touched; no other app reads this app's data.
- **No new third-party dependencies**; standard library only (Docker base image adds `tzdata` as an OS package).

## Cross-app impact
**INTEGRATION REVIEW REQUIRED**

Why: no contract or shared data changes, but this PR (a) adds a fourth copy of the shared sign-in modules listed under "Shared code" in APPS.md, so every future fix to `google_signin.py`/`auth.py` must also reach this app; (b) deliberately makes the gate stricter than Awards (differences 1-6 above), and a reviewer should decide whether those belong back in the other copies; (c) adds a hostname to the Cloudflare tunnel shared by every public app.

Affected: **Awards, Budget, Thank You** (same module copies; no code change in them here), **all tunnel-published apps** (Board Docs, Vendor Map, Incident Report, Budget, Awards, Thank You: one new tunnel row only), **dev-standards APPS.md** (separate PR: add Door Control row `hcf-doorcontrol`, `/srv/fair-apps/door-control`, 8480, doors.jakemc.me, git checkout, 1.0.0; add Door Control to "Shared code").

Seen in Awards while reading (report as DRIFT there, not fixed here): `.dockerignore` does not list `.env`; `/api/logout` has no CSRF check; `/health` answers any fair-network request; `tests/test_google.py` sets an unused `REMOTE_HOSTS`.

## Deploy notes
- **Version-bump files (from now on):** `VERSION`, `image: door-control:X.Y.Z` in `compose.yaml`, README title, `CHANGELOG.md`. Written into CLAUDE.md "Conventions".
- **Dockerfile COPY:** `run.py manage.py VERSION` + the whole `doorcontrol/` folder. Any new top-level file must be added there.
- **Migration:** automatic on first start (additive). Backup taken by the deploy block before anything changes.
- **Do not deploy** within 15 minutes of a schedule or calendar window starting/ending, or during the fair. The scheduler is offline for about 2 minutes and catches up on start (existing behavior, door_state is carried over in the copied database).

### Prerequisites (before deploy, in this order)
**P1. Google OAuth client (Jake, browser, personal Google account)**
1. https://console.cloud.google.com -> project picker -> **New project** -> name `HCF Door Control` -> Create, then select it.
2. **APIs & Services -> OAuth consent screen** (Google Auth Platform): App name `Fairgrounds Door Control`, support email = yours; **Audience: External**; add no scopes beyond `openid`, `email`, `profile`; then **Publish app** (status "In production"; these basic scopes need no Google verification. In "Testing" only listed test users could sign in).
3. **Clients -> Create client -> Web application**, name `Door Control`, **Authorized redirect URI** `https://doors.jakemc.me/api/auth/google/callback` (exactly). Create, and keep the Client ID and Client secret open for step P2.
4. Recommended: **IAM & Admin -> Grant access** -> Kolt's Google account, role **Owner**, so the client is not tied to one person.

**P2. `.env` on fair-server** (prompts for the two values; nothing to edit by hand):
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
ALLOWED_HOSTS=192.168.1.160:8480,fair-server:8480
EOF
ls -l .env)
```

### Deploy block (Jake, after the PR merges)
```bash
(set -e
cd /srv/fair-apps/door-control
test -f .env || { echo "Stop: create .env first (step P2)."; exit 1; }
mkdir -p /srv/fair-apps/door-control-backups && chmod 700 /srv/fair-apps/door-control-backups
test -e /srv/fair-apps/door-control-backups/pre-docker-commit || git rev-parse HEAD > /srv/fair-apps/door-control-backups/pre-docker-commit
BK=/srv/fair-apps/door-control-backups/$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$BK"
sudo systemctl stop door-control
sudo systemctl disable door-control
if systemctl is-active --quiet door-control; then echo "Stop: the old door-control service is still running."; exit 1; fi
cp -p data/doorcontrol.db config.json "$BK"/
umask 077
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); c.pop("web",None); c.pop("db_path",None); json.dump(c,open(sys.argv[2],"w"),indent=2)' "$BK/config.json" "$BK/config.docker.json"
git checkout main
git pull --ff-only
docker compose build
docker compose create
docker run --rm --user 0 -v door_control_data:/data -v "$BK":/src:ro door-control:1.0.0 sh -c 'if [ -e /data/doorcontrol.db ]; then echo "The volume already has a database; not overwriting it."; exit 1; fi; cp /src/doorcontrol.db /data/doorcontrol.db && cp /src/config.docker.json /data/config.json && chown 10001:10001 /data /data/doorcontrol.db /data/config.json && chmod 600 /data/config.json /data/doorcontrol.db'
docker compose up -d
sleep 30
docker compose ps
docker compose logs --tail=40
echo "Backup in $BK")
```
`docker compose ps` must show `healthy`. Never run `docker compose down -v`. The old `data/` folder and `config.json` stay where they are.

**Break-glass account (right after deploy; type a long random password, e.g. from a password manager, 20+ characters):**
```bash
(cd /srv/fair-apps/door-control && docker compose exec app python manage.py set-password admin && docker compose exec app python manage.py users)
```

**P3. Cloudflare public hostname - only after the container is healthy** (adding it while the old Basic-auth app ran would have put that app on the internet). Cloudflare Zero Trust -> Networks -> Tunnels -> the fair-server tunnel -> Configure -> **Public Hostname -> Add a public hostname**: Subdomain `doors`, Domain `jakemc.me`, Service type `HTTP`, URL written the same way as the existing `awards.jakemc.me` row but with port **8480** (e.g. `localhost:8480`). Save. Do not edit any other row; afterwards open awards.jakemc.me once to confirm the other apps still load.

**Manual checks on fair-server (AC24, AC11 Cloudflare route):** `https://doors.jakemc.me` shows the sign-in page; Google sign-in with a hardincountyfair.org account reaches the dashboard and the email shows in the header; a gmail.com account is refused; door list loads (UniFi reachable from the container); one "Unlock 1 hr" on a test door shows your email in Recent activity, then "Back to normal"; a Telegram `/doors` answers (outbound reachable, only one bot copy); `http://192.168.1.160:8480` shows the sign-in page only (**SUPERSEDED in round 3: it must now be unreachable, connection refused**); `curl -s -H 'Host: other.jakemc.me' -H 'CF-Connecting-IP: 1.2.3.4' http://127.0.0.1:8480/api/doors` returns 404; admin sign-in works at doors.jakemc.me.

### Later deploys (goes into CLAUDE.md)
```bash
(set -e; cd /srv/fair-apps/door-control && git checkout main && git pull --ff-only && docker compose up -d --build && docker compose ps)
```

## Risks
| Risk | Mitigation |
|------|------------|
| Two copies running (both poll the Telegram bot, both run the scheduler and fight over door state, both want port 8480) | Deploy block stops **and** disables the service, checks it is inactive, and aborts otherwise; port 8480 conflict would also stop the container from starting. Rollback block stops the container before starting the service. |
| Doors left unlocked/locked wrong across the switch | Service stopped before copying, so `door_state` is consistent; copied DB carries it into Docker; deploy outside window boundaries; rollback copies the volume DB back so the old version knows what the scheduler applied. |
| Lockout (Google misconfigured, OAuth client deleted, domain policy wrong) | Break-glass admin account created right after deploy and tested; scheduler and Telegram never depend on sign-in (AC6); UniFi's own app remains the last resort. |
| OAuth client on Jake's personal Google account (one person, could be lost/changed) | Add Kolt as project Owner (P1.4); README documents how to recreate the client (new values in `.env`, restart); break-glass covers the gap. |
| Shared Cloudflare tunnel: a bad edit breaks every public app | Add one row only, copy the format of the awards row, check another app afterwards. Add the row only after the container is healthy; remove it first on rollback. |
| Old app exposed to the internet | Tunnel row added only after Docker is healthy; rollback removes the row before restarting the systemd app. |
| No dashboard when internet/Cloudflare is down (cookies are `Secure`, LAN is plain HTTP) | Scheduler keeps running locally; UniFi app on the LAN works; see open question. |
| Break-glass brute force (a LAN client can rotate `CF-Connecting-IP` to dodge the per-address limit, same as Awards) | scrypt slows each guess; README requires a long random password; failures printed to the log. |
| `zoneinfo` data missing in the slim image breaks the scheduler | `tzdata` installed and checked at build time (build fails instead of runtime). |
| Container cannot reach UniFi (LAN) or Telegram/Google | Default bridge network NATs to the LAN and internet; manual check in the deploy checklist; rollback is quick. |
| Config files with tokens copied around | Backup folders mode 700, `umask 077`, volume files mode 600; `.env`, `config.json`, `data/` in `.gitignore` and `.dockerignore`. |
| Volume wiped by mistake | Fixed volume name `door_control_data`; `down -v` forbidden in README/CLAUDE.md; dated host backups kept. |
| Sign-in log rows push out older door rows (1000 cap) | Accepted per spec; noted in README. |

## Test plan
Automated: `tests/test_google.py` (new, unittest classes below), `tests/test_all.py` (existing), `tests/test_ui.cjs` (new). The server under test runs with `PUBLIC_HOSTS=app.test`, `ALLOWED_HOSTS=lan.test`, a temp `DATA_DIR`, the mock UniFi controller and a faked Google token endpoint (as Awards). `ROUTES` in the test is the list of every method+path in `web.py`, and a test asserts it matches the routes `web.py` declares, so a new route cannot skip AC1.

| Acceptance criterion | Test |
|----------------------|------|
| AC1 every `/api` route 401, no data, no UniFi call | `test_google.GateTests.test_every_api_route_needs_sign_in` (all methods/routes, remote and LAN hosts; asserts mock `calls` unchanged) + `test_route_list_matches_web` |
| AC2 sign-in page, static allow-list | `GateTests.test_signin_page_and_static_allowlist` |
| AC3 forged/tampered/expired/wrong-policy cookie | `GateTests.test_bad_session_cookies_rejected` |
| AC4 CSRF + Origin on every change route | `GateTests.test_changes_need_csrf_and_same_origin` (door action, schedule POST/PUT/DELETE, logout; asserts DB and mock unchanged) |
| AC5 Basic password not accepted, removed | `GateTests.test_basic_auth_not_accepted`; `RepoTests.test_no_web_password_anywhere` (config.example.json, app.py check); `test_all.WebTests.test_basic_header_refused` |
| AC6 scheduler + Telegram with no session | `IndependenceTests.test_tick_and_bot_without_session` (web server running, no cookie) |
| AC7 500 body is plain | `GateTests.test_server_error_hides_exception_text` (engine.status patched to raise) |
| AC8 good Google token | `GoogleTests.test_workspace_account_signs_in` |
| AC9 each refusal: 403, no cookie, logged | `GoogleTests.test_refusals` (subTest per case: other domain, unverified, no `hd`, wrong `aud`, replayed nonce, state mismatch, state > 10 min, `error` param; `assertLogs`) |
| AC10 outsider refused with `GOOGLE_ALLOWED_EMAILS` unset | `GoogleTests.test_outside_account_refused_without_allowed_emails` |
| AC11 host lockdown, start only on public host, LAN button | `HostTests.test_other_cloudflare_host_404`, `test_google_start_not_on_lan`, `test_lan_needs_signin_and_points_to_public_host`; Cloudflare route itself: manual check |
| AC12 policy change ends sessions | `GoogleTests.test_allowed_domain_change_ends_sessions` |
| AC13 Google not configured | `GoogleTests.test_not_configured_404_and_app_runs` (+ tick/bot still work) |
| AC14 cookie flags | `SessionTests.test_cookie_flags` (session and state cookies) |
| AC15 sign-out | `SessionTests.test_sign_out_needs_csrf_and_clears_cookie`; `test_ui.cjs` "header shows email and Sign out; sign out posts with CSRF token" |
| AC16 one constant, expiry, README | `SessionTests.test_lifetime_constant_and_expiry` (both modules use `SESSION_DAYS`; cookie 1 s past expiry rejected via patched time); `RepoTests.test_readme_documents_sign_everyone_out` |
| AC17 10 fails/15 min then 429 | `BreakGlassTests.test_rate_limit` |
| AC30 password sign-in | `BreakGlassTests.test_password_signin_and_log_row`; `test_ui.cjs` "Admin sign-in link, not main button" |
| AC31 storage + CLI | `BreakGlassTests.test_accounts_file_and_cli` (subprocess `manage.py` with temp `DATA_DIR`: mode 600, scrypt only, < 12 refused, `users`, `remove-user`; no `/api/accounts` route; password not in log output) |
| AC32 off without accounts | `BreakGlassTests.test_password_off_without_accounts`; `test_ui.cjs` "link hidden when password is false" |
| AC18 email on door actions, empty for scheduler/Telegram | `ActivityTests.test_who_did_it` |
| AC19 schedule changes logged | `ActivityTests.test_schedule_changes_logged` (add, edit, pause, resume, delete) |
| AC20 sign-in row | `ActivityTests.test_signin_logged` (refusal writes no row) |
| AC21 migration + By column | `ActivityTests.test_migration_is_additive` (old-schema DB with rows/schedules; ids and values unchanged; old-style INSERT still works); `test_ui.cjs` "By shows user, else source" |
| AC22 all paths via `door_action`, cap 1000 | `ActivityTests.test_all_paths_log_and_cap` |
| AC23 CSP + no inline | `SecurityTests.test_headers_on_every_response`, `test_no_inline_script_or_style` (whitespace-tolerant regex over served HTML and `app.js`/`login.js` strings); `test_ui.cjs` renders doors, schedule form, log |
| AC24 Docker | Manual on fair-server (deploy checklist). `SecurityTests.test_health_loopback_only` covers the `/health` rule. |
| AC25 secrets files | `RepoTests.test_ignore_files_and_env_example` |
| AC26 formatting, commit 1 identical results | Commands below (`black --check .`, `prettier --check`); commit-1 before/after `test_all.py` output pasted in the PR (manual) |
| AC27 version everywhere, COPY | `RepoTests.test_version_everywhere` (VERSION = compose tag = README title = CHANGELOG top; Dockerfile COPYs `run.py`, `manage.py`, `VERSION`, `doorcontrol`) |
| AC28 docs | Manual review in the PR; `RepoTests.test_readme_has_no_tailscale_or_password_text` |
| AC29 | This table; all three suites green |

Commands (go into CLAUDE.md):
```bash
docker run --rm -v "$PWD":/app -w /app python:3.13-slim-bookworm sh -c 'apt-get -qq update && apt-get -qq install -y tzdata >/dev/null && python tests/test_all.py && python tests/test_google.py'
docker run --rm -v "$PWD":/w -w /w node:22-alpine node tests/test_ui.cjs
docker run --rm -v "$PWD":/w -w /w python:3.13-slim-bookworm sh -c 'pip install -q black && black --check .'
docker run --rm -v "$PWD":/w -w /w node:22-alpine npx --yes prettier@3 --check "doorcontrol/static/*.{html,js,css}" "tests/*.cjs"
```

## Rollback
1. **First** remove the `doors.jakemc.me` row from the Cloudflare tunnel (Public Hostname -> delete that row only), so the old password-only app is never on the internet.
2. Run (copies the Docker database back so schedules and scheduler door state made since the deploy are kept; the old version reads it, the `user` column is additive):
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
The old `config.json` (with `web.password`) was never changed. The volume stays (no `-v`). To go forward again later, the normal deploy block works (`git checkout main` is in it); the volume already holds a database, so only `docker compose up -d --build` is needed.

## Open questions for Jake
1. With `Secure` cookies (as the spec requires), the LAN address cannot hold a session, so if the internet or Cloudflare tunnel is down there is no dashboard (scheduler keeps running; the UniFi app on the fair network still works). Accept, or allow break-glass sign-in on the LAN address with a non-`Secure` cookie (as Awards does)? Plan assumes accept.

## Addendum: Jake's approval and review fixes (2026-11-27)
Jake approved the plan with these decisions:
1. LAN sign-in over plain HTTP is NOT supported (Secure cookies stay). With the internet or tunnel down there is no
   dashboard; the scheduler and Telegram bot keep running; UniFi's own Access app is the fallback. Say so in the README.
2. One PR (the black/prettier reformat stays its own first commit).
3. Budget and Thank You were not compared. Copy `google_signin.py` and `auth.py` from Awards (record the Awards commit
   and sha256 in CLAUDE.md, plus a test that fails if the copies change). Jake will have the dev team compare the other
   apps' copies later; flag this as "not yet compared" in the PR description.

Fixes from INTEGRATION.md the developer must apply (IDs refer to that file):
- I1: pin Awards commit + sha256 of both copied modules in CLAUDE.md and test it.
- I2: deploy block = pull and build first, THEN stop/disable the systemd service, then start the container; never leave
  the doors without a scheduler if the build fails.
- I3: create the break-glass account before the Cloudflare hostname step; README gets an "If you cannot sign in" section.
- I5: Cloudflare step starts with a check for an existing `doors` record or wildcard; copy the awards row's format
  exactly; keep the catch-all rule last.
- I6: define the login/error page classes in static/style.css and include its static files (style.css, login.js,
  fair-logo.png) in the unauthenticated allow-list, with a test.
- I7-I9: add `localhost:8480` and `127.0.0.1:8480` to the allowed hosts and say what an empty value means; run
  manage.py only as the container user, never root.
Not in this PR: I10 (Awards drift) and I11 (APPS.md / STANDARDS.md); report them to Jake as follow-up PRs.
