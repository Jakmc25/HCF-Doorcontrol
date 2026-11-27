# Spec: Google sign-in for the dashboard (and bring the app in line with the standards)

Size: Large (sign-in, outside service, secrets, public address, new deploy method).
Branch: feature/google-signin. Jake approved combining the drift fixes with this feature in one PR.

## Problem
- The dashboard is protected by one shared HTTP Basic password. Anyone who knows it is anonymous; the activity log
  says only "web", never who unlocked a door or changed a schedule.
- It cannot be reached safely from outside the fairgrounds (README says "never expose it"). The standards require
  remote management by sign-in, never LAN-only.
- Known DRIFT (CLAUDE.md): systemd instead of Docker Compose, no CSP, no CSRF, inline script/style, no
  black/prettier, no VERSION file.

Affects: Jake and Kolt (deploy), every hardincountyfair.org person who runs doors, and the fairgrounds' physical security.

## Goal
Anyone with a hardincountyfair.org Google account can sign in at https://doors.jakemc.me, from anywhere, to view and
control doors and schedules, and every action is recorded with their email. Nobody else can reach any `/api` route.
The app runs in Docker Compose and follows the security and style standards. Scheduler and Telegram bot are unchanged.

## Decisions already made (not open)
- Only verified `hardincountyfair.org` accounts (token `hd` claim must equal the email domain). No allow-list of people:
  every such account may view and control.
- Public hostname `doors.jakemc.me` through the existing Cloudflare tunnel. Google sign-in works on that hostname only;
  Cloudflare traffic with any other Host is refused. New Google OAuth client under Jake's personal Google account,
  redirect URI `https://doors.jakemc.me/api/auth/google/callback`.
- Sign-in is the only gate; nothing is LAN-only. The fair network needs sign-in too (as Awards 1.5.0).
- Telegram bot unchanged (its own allow-list by chat ID).
- Copy of Awards' `google_signin.py` and `auth.py` (copies, not a library; Awards at /home/claude/hcf_awards is the
  reference and is not modified). Keep names, cookie rules, state/nonce/`hd` checks, rate limit, CSRF and CSP
  headers consistent with Awards `server.py`. APPS.md "Shared code" must list Door Control as another copy.

## Acceptance criteria

### Sign-in gate and safety
- [ ] AC1: Given no session cookie, when any request is made to any `/api/...` route (GET, POST, PUT, DELETE, every
  method and route currently in web.py), then the response is 401 JSON, no door/schedule/log data is returned and
  no UniFi call is made. Test enumerates every route.
- [ ] AC2: Given no session, when `GET /` is requested, then a sign-in page is returned that contains no door data;
  only the files it needs (login script and CSS) are served without a session; all other `/static/` files return 401/404.
- [ ] AC3: Given a forged, tampered, expired or wrong-policy session cookie, when `/api/doors` is requested, then 401.
- [ ] AC4: Given a POST/PUT/DELETE with a valid session but no/wrong CSRF token, or an `Origin` that does not match
  `Host`, when any change route is called (door action, schedule add/edit/delete), then it is refused (403) and nothing changes.
- [ ] AC5: Given the HTTP Basic header with the old `web.password`, when any route is requested, then it is not
  accepted. `web.password` is removed from config, `config.example.json`, and the startup check.
- [ ] AC6: Given nobody is signed in, when the scheduler tick runs and when a Telegram command from an allowed
  chat ID arrives, then both work exactly as today (tests call `Engine.tick` and `Bot.handle` with the web server
  running and no session).
- [ ] AC7: Given an unexpected server error, when the response is sent, then the body is a plain sentence and
  does not include the exception text.

### Google sign-in
- [ ] AC8: Given a Google token with `iss` Google, `aud` = our client ID, unexpired, matching nonce/state,
  `email_verified` true, and `hd` = `hardincountyfair.org` for an `@hardincountyfair.org` email, when the callback
  is hit, then a session cookie is set and the person lands on the dashboard (fake Google token endpoint, as in
  Awards `tests/test_google.py`).
- [ ] AC9: Given each of: other domain (e.g. gmail.com), email not verified, `hd` missing, wrong `aud`, replayed
  nonce, state not matching the browser's state cookie, state older than 10 minutes, Google `error` param, then
  sign-in is refused (403 page, plain message), no session cookie is set, and the attempt is printed to the server log.
- [ ] AC10: Given `GOOGLE_ALLOWED_EMAILS` is unset (the deployed setting), when a non-hardincountyfair.org
  account signs in, then it is refused. (Optional extra-emails support is kept from the Awards copy; it is off unless set.)
- [ ] AC11: Given a request with Host other than `doors.jakemc.me` that arrives through Cloudflare (has
  `CF-Connecting-IP`), when any route is requested, then 404. Given `/api/auth/google/start` on a non-public host, then no
  redirect to Google. Given a request on the fair-network address, then sign-in is still required and the Google
  button points to `https://doors.jakemc.me/api/auth/google/start`.
- [ ] AC12: Given the allowed-domain setting is changed and the app restarted, when an old session is used, then it
  is rejected (policy hash, as Awards).
- [ ] AC13: Given Google sign-in is not configured (no client ID/secret), when `/api/auth/google/start` is
  requested, then 404 with a plain message, and the app still starts and the scheduler and Telegram run.

### Session, sign-out, cookies
- [ ] AC14: Given a successful sign-in, then the session cookie has `HttpOnly`, `Secure`, `SameSite=Strict`,
  `Path=/` and a `Max-Age`; the pre-callback state cookie is `HttpOnly; Secure; SameSite=Lax`, `Path=/api/auth/google/`,
  10 minutes.
- [ ] AC15: Given a signed-in person, when they press Sign out (a POST with CSRF token), then the cookie is cleared
  (`Max-Age=0`) and the dashboard shows the sign-in page. Sign-out button and the signed-in email are visible in the header.
- [ ] AC16: Session lifetime is set in one constant (default 14 days, same as Awards); a cookie one second past
  expiry is rejected. Sessions are stateless: sign-out clears the browser cookie only; "sign everyone out" = delete
  `/data/session-secret` and restart (document in README).
- [ ] AC17: Failed break-glass password attempts are limited to 10 per address
  per 15 minutes, then 429.
- [ ] AC30 (break-glass): Given an admin account created on the server with `python3 manage.py set-password admin`,
  when someone submits that name and password on the sign-in page, then they get the same kind of session as a
  Google sign-in (AC14, AC16) and a log row "signed in (password)" with the account name; a wrong password gets a
  plain error and no session. The password form appears under a small "Admin sign-in" link, not as the main button.
- [ ] AC31 (break-glass storage): Passwords are stored only as scrypt hashes in `/data/accounts.json` (file mode 600),
  never in `config.json`, `.env` or logs. `manage.py` offers `set-password NAME`, `users`, `remove-user NAME`; passwords
  shorter than 12 characters are refused. There is no way to create or change a password from the web.
- [ ] AC32 (break-glass off by default): Given no account exists, the password sign-in endpoint always refuses and the
  "Admin sign-in" link is hidden.

### Who did it (activity log)
- [ ] AC18: Given a signed-in person presses any door action, when it succeeds or fails, then the activity log row
  has their email in a new `user` column; scheduler rows have `source` "scheduler: ..." and an empty `user`;
  Telegram rows have `source` "telegram" and an empty `user`.
- [ ] AC19: Given a signed-in person adds, edits, pauses/resumes or deletes a schedule, then a log row is written
  with their email and a plain description including the schedule name (schedule changes were not logged before).
- [ ] AC20: Given a successful Google sign-in, then a log row "signed in" with the email is written. Sign-in
  refusals go to the server log only.
- [ ] AC21: Given the existing `data/doorcontrol.db`, when the new version starts, then the `user` column is added by an additive
  migration (no table recreate), old rows and schedules are unchanged, and the dashboard "By" column shows `user`
  when present, else `source`.
- [ ] AC22: Door actions still go only through `Engine.door_action` (new optional `user` argument); web, Telegram and
  scheduler paths all still produce a log row. The 1000-row log cap is unchanged (note: sign-in rows share it).

### Standards drift (fixed in this PR)
- [ ] AC23 (CSP): Every response carries `Content-Security-Policy` with `script-src 'self'; style-src 'self'`
  (plus `frame-ancestors 'none'`, `X-Content-Type-Options`, `Referrer-Policy`, as Awards). `index.html` contains no
  inline `<script>`, `<style>`, `style=""` or `on...=""`; JS and CSS are in `static/app.js`, `static/style.css`
  (and `login.js`). Test greps the served HTML and the JS-built HTML strings with whitespace-tolerant patterns.
  The dashboard still works (Node UI test of doors, schedule form, log).
- [ ] AC24 (Docker): `docker compose up -d --build` starts the app with `read_only: true`, non-root user, volume
  `door_control_data` at `/data` holding `config.json`, `doorcontrol.db` and `session-secret`, tmpfs for `/tmp`,
  published port 8480, healthcheck on `/health` (answered only for requests from inside the container, no sign-in).
  `docker compose ps` shows healthy. UniFi (LAN) and Telegram/Google (outbound) are reachable from the container.
- [ ] AC25 (secrets): `.env` holds `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_ALLOWED_DOMAINS=hardincountyfair.org`,
  `PUBLIC_HOSTS=doors.jakemc.me`, `ALLOWED_HOSTS` (fair-server LAN address); `.env`, `config.json` and `data/` are in
  `.gitignore` and `.dockerignore`; `.env.example` lists them with no real values.
- [ ] AC26 (format): `black --check .` passes for Python and `prettier --check` passes for the app's own
  JS/CSS/HTML, with `.prettierrc.json` as in Awards (HTML strict whitespace). Reformatting is its own first commit
  in the PR (no logic change; `tests/test_all.py` results identical before and after).
- [ ] AC27 (version): `VERSION` file added (1.0.0, the first release); version also in compose `image: door-control:1.0.0`,
  README title and a CHANGELOG entry; bump rules written into CLAUDE.md "Conventions". All new files are in the Dockerfile `COPY`.
- [ ] AC28 (docs): CLAUDE.md updated (Where it runs: Docker Compose, doors.jakemc.me, deploy command as one copy-paste block,
  tests command, History, Known DRIFT section removed or reduced to what remains). README rewritten: sign-in
  instead of the password/Tailscale text, `.env` lines, OAuth client setup, restore instructions.

### Tests
- [ ] AC29: `tests/test_google.py` (modelled on Awards') covers AC1-AC4, AC8-AC17, AC18-AC20, AC30-AC32; existing `tests/test_all.py`
  still passes; every AC above has a test or a stated manual check (AC24 and the Cloudflare route are manual on fair-server).

## Move from systemd to Docker (data safety)
- Two copies must never run together: both poll the same Telegram bot and both want port 8480. The deploy block
  stops and disables the `door-control` service first.
- Before the move: copy `data/doorcontrol.db` and `config.json` to a dated backup, then copy them into the new volume.
  Never delete the old `data/` or the new volume (`docker compose down -v` forbidden).
- Rollback: `docker compose down` (no `-v`), `sudo systemctl enable --now door-control`; the old folder and database are untouched.
  The new `user` column is additive, so the old version still reads the copied database.
- Deploy block (Jake) will be one complete copy-paste block in the PR description and CLAUDE.md.

## Prerequisites outside the code (done before deploy; listed in the PR)
1. Jake creates the Google OAuth client (web application) under his personal Google account, consent screen set for
   the `hardincountyfair.org` Workspace users, redirect URI above. Note: if the consent screen is "External",
   `hd` enforcement in our code still applies; "Internal" is not available on a personal account.
2. Add `doors.jakemc.me` to the Cloudflare tunnel pointing at port 8480 on fair-server (the tunnel is shared with all
   apps: edit only the new hostname).
3. Add `.env` on the server with the lines in AC25.

## Out of scope
- Scheduling logic, UniFi API behavior, Calendar sync, Telegram bot behavior and its allow-list.
- Per-person permissions, view-only users, an in-app user list.
- Server-side session revocation or per-session list.
- Adding a UniFi door-open "who" mapping beyond the email in our own log.
- Editing Awards, the standards or APPS.md (APPS.md needs a Door Control row and a "Shared code" mention: separate
  change in dev-standards).

## Other apps affected
- Awards: none (read-only reference; its modules are copied). Budget and Thank You: none, but future sign-in fixes in
  the shared modules now also belong in this app.
- Cloudflare tunnel (shared by all apps): one new hostname; a bad edit could affect every app's public address.
- FairSync and other Telegram bots: none; this app keeps its own separate bot token (two programs cannot poll one bot).
- APPS.md in dev-standards: add Door Control (repo hcf-doorcontrol, /srv/fair-apps/door-control, 8480, doors.jakemc.me, 1.0.0, Docker).

## Decisions (Jake, 2026-11-27)
1. Break-glass admin password: **yes**. One password account managed only from a CLI command on the server (no in-app
   account screen), scrypt hash in `/data`, rate limited (AC17), available on the same hostname. Cost accepted: a second
   credential on an internet-facing door app. The README must say to use a long random password.
2. Session lifetime: **14 days** (same as Awards).
3. Spec approved to move to PLAN.

## Open questions
None.
