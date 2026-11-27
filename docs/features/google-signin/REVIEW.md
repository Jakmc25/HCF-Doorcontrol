# Review: Google sign-in (round 1)

Reviewers: code-reviewer (PASS, C1 should-fix), qa-tester (CHANGES REQUIRED, Q1), security-reviewer (CHANGES REQUIRED, S1).
Tests at review time: test_all 16, test_google 41, test_ui 12 (all pass). Q1 is the same defect as S1. Round 2 re-check: Q1, C1, S1-S4 fixed; S11 and D1 added.

| ID | Level | Where | Finding | Status |
|----|-------|-------|---------|--------|
| S1/Q1 | BLOCKER | web.py ip(), remote-mode check; compose.yaml ports | `CF-Connecting-IP` and `Host` are trusted from any client. A LAN client can rotate a fake header to bypass the 10-per-15-min break-glass limit (AC17) and grow `_fails` without bound. | fixed: TRUSTED_PROXIES (default loopback + 172.16.0.0/12), own FailureLimiter (10/address, 50 overall, pruned); tests TrustTests |
| S2 | SHOULD-FIX | web.py `_body` | Negative `Content-Length` reaches `rfile.read(-1)` on `/api/login` without sign-in. | fixed: negative/non-numeric Content-Length -> 400; test_bad_content_length_is_400 |
| S3 | SHOULD-FIX | unifi.py, config | UniFi connection does not verify the console certificate (token capture on the LAN). | fixed: optional unifi.cert_sha256 checked before the request is sent; startup warning when unset; UnifiTlsTests (mock over HTTPS) |
| S4 | SHOULD-FIX | web.py server | No socket timeout or connection cap. | fixed: Handler.timeout 30 s, max 64 connections (extra closed at once); tests |
| C1 | SHOULD-FIX | CLAUDE.md first-move block | `exit 1` does not fire the ERR trap, so the rollback never runs and the old service stays stopped. | fixed: set -eE, restore_old() called by the ERR trap and explicitly on every exit path, plus a 90 s health check; test |
| S6 | NIT | web.py log | Break-glass account names can look like a staff email in the log. | fixed: admin:NAME in header and log |
| S8 | NIT | unifi.py, web.py | Raw UniFi / calendar error text shown to signed-in users. | fixed: plain UniFi/calendar/scheduler sentences, details to server log |
| S9 | NIT | ical.py | Calendar feed read unbounded; http:// accepted. | fixed: https:// required at startup (config message), 5 MB cap |
| S10 | NIT | compose.yaml | No cap_drop / pids / memory limit. | fixed: cap_drop ALL, pids_limit 128, mem_limit 256m |
| C2 | NIT | CLAUDE.md backup step | WAL/SHM files not copied if the old DB is in WAL mode. | fixed: SQLite backup API in the move, rollback and backup blocks |
| C4 | NIT | compose.yaml, blocks | `door-control:1.0.0` hardcoded in deploy/rollback blocks. | fixed: docker compose run instead of the image tag; test |
| Q2 | NIT | web.py | PATCH/HEAD/OPTIONS return Python's default 501 HTML. | fixed: 405 JSON for PATCH/OPTIONS/HEAD, JSON send_error for the rest |
| S5 | NIT | auth.py | `set_password` accepts 10 chars (spec says 12); only manage.py enforces 12. No web route sets passwords. | not fixed: shared copy pinned to Awards; follow-up |
| S7 | NIT | google_signin.py | No PKCE; state cookie not cleared after callback. | not fixed: shared copy pinned to Awards; follow-up for all apps |
| C3 | NIT | *.md | `prettier --check .` flags Markdown (AC26 covers JS/CSS/HTML only). | not fixed |
| C5 | NIT | .dockerignore | `.env.*` also excludes `.env.example`. | not fixed (harmless) |
| S11 | SHOULD-FIX (round 2) | compose.yaml, web.py | Default TRUSTED_PROXIES 172.16.0.0/12 trusts every container on fair-server. | fixed (round 3): port published on 127.0.0.1:8480 only (containers cannot reach host loopback; cloudflared uses 127.0.0.1:8480), TRUSTED_PROXIES stays the door-control-net gateway /32; docs say dashboard only via doors.jakemc.me, fair-network check = cannot connect; re-publish warning; test_compose_publishes_on_loopback_only |
| N1 | NIT (round 3) | README, CLAUDE.md | Cert-pin block breaks on an IPv6 unifi.host; leftover run.sh. | fixed: brackets for IPv6 (test_pin_block_handles_ipv6_console); run.sh deleted |
| D1 | DOCS (round 2) | CLAUDE.md, README | Deploy steps should pin the UniFi certificate. | fixed: first-move block reads the fingerprint before stopping the old service and saves unifi.cert_sha256; routine deploy prints a reminder; README pin block; console address read from unifi.host |

Checked and clean (QA/code/security): 401 on all /api routes and non-public static files; /static traversal; old Basic password refused;
forged/missing CSRF and foreign Origin refused; tampered cookies refused; accounts.json mode 600; additive log.user migration on an old DB;
scheduler and Telegram work with nobody signed in; build-then-stop deploy order is safe.
