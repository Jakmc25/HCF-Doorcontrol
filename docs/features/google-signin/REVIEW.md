# Review: Google sign-in (round 1)

Reviewers: code-reviewer (PASS, C1 should-fix), qa-tester (CHANGES REQUIRED, Q1), security-reviewer (CHANGES REQUIRED, S1).
Tests at review time: test_all 16, test_google 41, test_ui 12 (all pass). Q1 is the same defect as S1.

| ID | Level | Where | Finding | Status |
|----|-------|-------|---------|--------|
| S1/Q1 | BLOCKER | web.py ip(), remote-mode check; compose.yaml ports | `CF-Connecting-IP` and `Host` are trusted from any client. A LAN client can rotate a fake header to bypass the 10-per-15-min break-glass limit (AC17) and grow `_fails` without bound. | open |
| S2 | SHOULD-FIX | web.py `_body` | Negative `Content-Length` reaches `rfile.read(-1)` on `/api/login` without sign-in. | open |
| S3 | SHOULD-FIX | unifi.py, config | UniFi connection does not verify the console certificate (token capture on the LAN). | open |
| S4 | SHOULD-FIX | web.py server | No socket timeout or connection cap. | open |
| C1 | SHOULD-FIX | CLAUDE.md first-move block | `exit 1` does not fire the ERR trap, so the rollback never runs and the old service stays stopped. | open |
| S6 | NIT | web.py log | Break-glass account names can look like a staff email in the log. | open (cheap, include) |
| S8 | NIT | unifi.py, web.py | Raw UniFi / calendar error text shown to signed-in users. | open (cheap, include) |
| S9 | NIT | ical.py | Calendar feed read unbounded; http:// accepted. | open (cheap, include) |
| S10 | NIT | compose.yaml | No cap_drop / pids / memory limit. | open (cheap, include) |
| C2 | NIT | CLAUDE.md backup step | WAL/SHM files not copied if the old DB is in WAL mode. | open (cheap, include) |
| C4 | NIT | compose.yaml, blocks | `door-control:1.0.0` hardcoded in deploy/rollback blocks. | open (cheap, include) |
| Q2 | NIT | web.py | PATCH/HEAD/OPTIONS return Python's default 501 HTML. | open (cheap, include) |
| S5 | NIT | auth.py | `set_password` accepts 10 chars (spec says 12); only manage.py enforces 12. No web route sets passwords. | not fixed: shared copy pinned to Awards; follow-up |
| S7 | NIT | google_signin.py | No PKCE; state cookie not cleared after callback. | not fixed: shared copy pinned to Awards; follow-up for all apps |
| C3 | NIT | *.md | `prettier --check .` flags Markdown (AC26 covers JS/CSS/HTML only). | not fixed |
| C5 | NIT | .dockerignore | `.env.*` also excludes `.env.example`. | not fixed (harmless) |

Checked and clean (QA/code/security): 401 on all /api routes and non-public static files; /static traversal; old Basic password refused;
forged/missing CSRF and foreign Origin refused; tampered cookies refused; accounts.json mode 600; additive log.user migration on an old DB;
scheduler and Telegram work with nobody signed in; build-then-stop deploy order is safe.
