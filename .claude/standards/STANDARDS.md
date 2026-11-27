# Team standards

Shared rules for every Hardin County Fair app. Change them here in dev-standards, then sync to apps.

**The standards win.** The goal is that all apps are built and released the same way.
Each app's CLAUDE.md is the quick reference for facts about that app (what it does, its files,
where it runs, its deploy command, tests, history) so Claude can get up to speed fast.

**Drift:** when an app's code or its CLAUDE.md doesn't match these standards, report it as DRIFT
and bring the app in line, in its own small pull request (`/fix` or a `chore/` branch), never mixed
into a feature, so feature changes stay easy to review and safe to deploy. If a standard looks wrong
for an app, propose changing the standard here instead of making a quiet exception.

## People
| Person | GitHub | Role |
|--------|--------|------|
| Jake | Jakmc25 | Owner. Uses the dev team, reviews and merges pull requests, deploys to fair-server. |
| Kolt | littlebuck17 | Maintainer. Uses the dev team, reviews and merges pull requests. |

- Either of them may approve a spec or plan and merge a pull request.
- **Deploys:** Jake. (When Kolt gets a fair-server login, add "deploys" to his row.)
- Everything written for "the person deploying" (server steps, .env lines) must work for anyone in this table.

## How the apps are built
- Python standard library HTTP server, no framework; as few dependencies as possible
  (the apps should keep running for years untouched)
- SQLite in a Docker volume or bind mount at /data
- Plain JavaScript front end, no build step, no npm
- Docker Compose on fair-server, read-only container and non-root user where the app already does so
- Python code style: **black** with default settings (4-space indent, one statement per line,
  88-character lines, double quotes). Decided by Jake 2026-11-09.
  - Run `black <files you changed>` before committing; `black --check .` must pass.
  - Never use the old compact style (one-space indent, `;`-joined statements), even next to old code.
  - Whole-app reformatting is done only in its own chore PR, never inside a feature.
- JavaScript, CSS and HTML style: **prettier** with default settings (2-space indent, semicolons,
  double quotes, 80-character lines), except HTML uses strict whitespace so formatting never changes
  page text. Each app pins this in `.prettierrc.json`. Decided by Jake 2026-11-09.
  - Run `prettier --write <files you changed>`; `prettier --check` on the app's own front-end files must pass.
  - Vendored libraries (e.g. `static/vendor/`) and generated files (e.g. Vendor Map's `static/manual.html`) are never formatted.
  - Tests must not depend on formatting: match code or CSS text with whitespace-tolerant patterns.

## Security (all apps)
- Strict CSP (`script-src 'self'; style-src 'self'`) where the app has it: no inline scripts,
  `onclick=""`, `<style>` or `style=""` in HTML strings
- CSRF protection on every change; escape everything rendered
- Google sign-in limited to hardincountyfair.org / allowed emails, at the app's public hostname only;
  Cloudflare traffic for other hostnames refused
- **Remote management (Jake's requirement, 2026-11-09):** everything an admin does — Settings,
  credentials, user management, status changes — must work from anywhere, not just the fair network.
  Protect admin functions with **sign-in** (Google, limited as above), never by restricting them to
  the LAN. "Fair network only" is acceptable for staff convenience features (like SoldIII's
  clerk/cashier password sign-in), never as the only way to reach an admin function.
- Apps that are open without sign-in by choice (Incident Report: filing reports) still require
  sign-in for Settings and anything that changes where data goes or who receives it.
- Never commit `.env`, passwords, App Passwords, credentials JSON, or real fair data
  (buyers, exhibitors, board records, reports, uploads)

## Data safety
- Never delete or recreate an app's data volume (`docker compose down -v` is forbidden)
- Schema changes are additive migrations; never edit an existing migration
- Anything that changes stored data needs a backup first and a plan for going back
- Sale-day / fair-time apps (SoldIII especially): reliability beats features; never deploy during a sale

## Errors and UI text
- Plain-language messages for volunteers; errors raised as `ValueError('Plain sentence.')` where the app uses that pattern
- Every change of note goes to the app's audit log where it has one

## Releases
- Every release bumps the version in every place the app's CLAUDE.md lists (e.g. `VERSION`,
  `core.py`, the `image:` tag in compose.yaml, README title, CHANGELOG, RELEASE_MANIFEST.json)
- New files must be added wherever the app's CLAUDE.md says (Dockerfile `COPY`, required-file
  lists, manifests), or the deploy will miss them
- New environment variables: document the exact `.env` / compose lines the person deploying must add
- Update the History section of the app's CLAUDE.md

## Deploying
- `main` is what should be on the server. Merging a pull request is the release decision.
- Deploys are run by the people marked "deploys" under People. Each app's CLAUDE.md "Where it runs" section has the exact command.
- Server commands are always complete copy-paste blocks; never ask anyone to edit a line by hand.

## Testing
- Every acceptance criterion has a test
- Run the app's test commands from its CLAUDE.md (usually in Docker, plus Node UI tests)

## Git
- Work on branches (feature/<slug>, fix/<slug>, chore/<slug>) and open pull requests
- Nobody (human or agent) pushes directly to main
