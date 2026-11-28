# Plan: Safety fixes and Telegram alerts (Door Control 1.0.0 to 1.1.0)

Spec: `docs/features/safety-alerts/SPEC.md` (approved, Decisions 2026-11-28). Audit: `docs/audits/2026-11-27.md`.
Branch: `feature/safety-alerts`. Size: Large. Recommended as **two PRs** (see "PR split" at the end).

## Approach

Ground rules from CLAUDE.md, all kept:
- Standard library only. No new dependency, no new top-level file (new modules live in `doorcontrol/`,
  which the Dockerfile already copies as a folder).
- `engine.py`, `ical.py`, `telegram_bot.py`, `unifi.py`, `db.py` and the new `notify.py`, `people.py`,
  `backup.py` never import `web`, `auth` or `google_signin`. Scheduler, bot, alerts and backups work with
  nobody signed in. The import-graph test is extended to the new modules.
- Every door action still goes through `Engine.door_action` (it gains the alert hook and outage tracking).
- Every new route goes in `web.ROUTES`. Schema changes only in `DB._migrate`, additive.
- `google_signin.py` and `auth.py` are **not edited**; the hash-pin test stays as it is. App rules go in
  `web.py` (see 6).
- Fail-safe everywhere: on doubt keep the current state, log, alert. Never touch a door changed by hand.

### 1. Notifier (`doorcontrol/notify.py`, new)
`Notifier(token, chat_ids, cfg_alerts, send=None, clock=time.time)`.
- `alert(kind, text, key=None)` is O(1): takes a lock, applies dedupe, appends to a bounded `deque`
  (`alerts.queue_size`, default 200) and notifies a `Condition`. No I/O, never raises (wrapped in
  `try/except Exception` with a log line). This is the only call the scheduler, calendar, bot and web make.
- Kinds and per-kind switches (`alerts.kinds.<kind>`, all default true, spec Decision 3): `door_error`,
  `scheduler`, `calendar`, `backup`, `schedule_change`, `status`, `hand_change`, `people`, `recovered`.
  A switched-off kind is logged only.
- Priority by kind: `status` and `schedule_change` are low; all others are high. Full queue: drop the
  oldest low-priority item and count it (`dropped_low`); if only high items are queued, drop the oldest
  high item **with a WARNING log line** and count it (`dropped_high`). Counters are shown in signed-in
  `/api/health` and added to the next summary message.
- Dedupe (AC33): `key = (kind, door_or_event, reason)`. First occurrence is queued. Repeats inside
  `alerts.repeat_minutes` (default 30, spec Decision 6) only bump `count`; the first repeat after the
  window is queued as "<text> (repeated N times since 14:05)". Keys idle for 24 h are pruned. Status
  changes use `key=None` (each is a distinct event, no dedupe).
- Sender thread (daemon, name `notify`): waits on the Condition, sends each alert to every allowed
  **private** chat (positive chat ids only; negative/group ids are skipped with one startup warning,
  AC36) via `sendMessage` (`urllib`, timeout 10 s). Cap (AC33): at most `alerts.max_per_minute`
  (default 20) alerts per rolling 60 s; past the cap, the remaining queued items are folded into one
  "N more alerts: first lines ..." message sent when the minute opens. Telegram errors: backoff 5 s to
  5 min, message stays queued (bounded by the deque), WARNING log line.
- Scrub: every outgoing text passes `scrub()`, which replaces the bot token, UniFi token and calendar
  URL (and any `calendar.google.com` URL) with `[hidden]`. Texts come from fixed templates anyway;
  scrub is the backstop (AC29).
- No token or no allowed chats (AC35): `alert()` logs at INFO and returns; no thread started.
- Recovery (AC34): `outage(name, text)` / `recovered(name, text)` pairs for `unifi`, `calendar`,
  `scheduler`; `recovered` sends one message only if an outage alert was sent.

Where each event hooks in:
| Event | Hook |
|---|---|
| Door lock/unlock/reset from any source (AC31) | `Engine.door_action` after a successful UniFi call: door, new state, source, who (`user`, `scheduler: <title>`, `calendar:<organizer>`), end time when known (`unlock_for` minutes, window end). |
| Door action fails (AC29) | Web/Telegram: `door_action` alerts at once (key door+action). Scheduler: after `alerts.failures_before_alert` (default 2) consecutive failed ticks for that door+action; `recovered` on success. |
| Hand change (AC25, AC31) | Engine reconcile (see 3). |
| Tick error / stall / per-door error (AC24, AC26) | `Engine.run_forever`, per-door `except`, watchdog thread. |
| Calendar fetch/parse problem, refused event, unknown door part, stale, startup "no organizers" (AC2, 3, 5, 8, 9, 13, 15, 18, 20) | `Calendar._loop` / `load_text`, `ical.expand` problem list, `Engine._windows` matching. |
| Schedule created/edited/deleted/paused/resumed (AC30) | New `Engine.add_schedule/update_schedule/delete_schedule(…, user)`; web calls these instead of `db.*` + `note`. Telegram has no schedule editing (new commands are out of scope), so only web paths exist today; the engine methods take `user` so any future path is covered. |
| People add/remove (AC41a) | `web.py` people routes. |
| Backup failure | `backup.py` thread. |

### 2. Calendar safety (`ical.py`, matching in `engine.py`)
Parsing (`_parse_events`):
- Read `ORGANIZER` (lower-cased `mailto:` address), `ATTENDEE` (address + `PARTSTAT`), `STATUS`.
- A property that fails to parse (bad DTSTART, unknown TZID, bad EXDATE, bad RRULE) marks the event
  `problem="..."` instead of being silently dropped or falling back. Unknown TZID is a problem (AC9),
  not a fallback to local time.
- `EXDATE;VALUE=DATE` values are stored as local dates, timed EXDATEs as UTC instants (AC12).

Acceptance, in `Calendar.windows()` (each refusal: log line with organizer and title, deduped alert
keyed by `(calendar, uid, reason)`):
- AC1: `STATUS` must be `CONFIRMED`; missing = refused. CANCELLED is logged at debug only (normal
  deletions stay quiet); other statuses alert.
- AC2/AC3: organizer must be in the allowlist. `people.allowed_organizers(cfg, db)`: if
  `calendar.allowed_organizers` is present, exactly that list; else admins (`GOOGLE_ALLOWED_EMAILS`)
  plus the in-app people list (read from the DB at each refresh). If both are empty and a feed is
  configured: no window ever, one startup alert "Calendar is set up but nobody is allowed to create
  unlock events". `people.py` reads env and DB only (no `web` import).
- AC4: invitation rule. "Our" address is `calendar.calendar_email` if set; if not set, every ATTENDEE
  that is not the organizer counts (fail-safe). If any of those has `PARTSTAT` NEEDS-ACTION or
  TENTATIVE, the event is refused even with an allowlisted organizer.
- AC5: a door part equal to `all` (any case) or `*` opens nothing, logged, alerted. Calendar input no
  longer goes through `_query_matches`' `*` rule (schedules keep their explicit `"*"` door list).
- AC18: window length (event plus buffers) over `calendar.max_window_hours` (default 24) is refused.
  All-day events are measured in calendar days (1 day = 24 h), so an all-day event on a DST day
  (23/25 real hours) still passes; a multi-day all-day event is refused (README: one event per day).
- AC17: all-day event = local midnight to next local midnight (wall clock), documented in README with
  the lookback (`ceil(max_window_hours/24) + 1` days back, so any allowed window is seen).

Door matching (AC6-8), new pure function `engine.match_doors(text, doors) -> (door_ids, problems)`:
1. Whole text equals a door `name` or `full_name` (casefold, trimmed) → that door only, no split.
2. Else split on `,`, `&`, `\band\b`; each part: exact name/full_name; else a partial (substring) match
   that hits exactly one door; zero or several → that part is skipped and reported ("'Main' matches
   Main Gate and Main Office"); other parts still apply.
`Calendar.windows()` returns `door_text`, `organizer`, `uid`; the engine matches against live doors.
The README "Unlock: ALL" line is removed.

Recurrence (AC9-12), `_rrule_occurrences` rewritten behind `validate_rrule(rule)`:
- `FREQ` in DAILY, WEEKLY, MONTHLY, YEARLY; anything else → problem.
- `INTERVAL` 1..1000 and `COUNT` 1..10000, digits only.
- `BYMONTHDAY`: each value 1..31, else problem (0, 32, 40 refused).
- WEEKLY `BYDAY`: plain codes only; numeric prefixes → problem.
- MONTHLY `BYDAY` (spec Decision 5): one code with prefix 1..4 or -1 (`2TU`, `1MO`, `-1FR`), or one
  plain code plus a single `BYSETPOS` 1..4 or -1, expanded as nth/last weekday of the month. Anything
  else (several codes, `5MO`, `-2FR`, BYSETPOS lists) → problem.
- Any other `BY*` part (BYHOUR, BYWEEKNO, BYYEARDAY, BYMONTH with MONTHLY, ...) → problem. `WKST` ignored.
- Unparseable `UNTIL` → problem (no longer silently "forever").
- Hard bound: at most `MAX_CANDIDATES = 5000` candidates **tried** per event (including invalid dates
  skipped), so no rule can loop forever.
- `expand()` wraps each event in `try/except Exception` (AC10): the event is dropped and reported; the
  rest continue. `Engine._windows` also wraps the whole calendar part, so schedules are always built.
- EXDATE: date-only values cancel any occurrence on that **local** date; timed values compare in UTC.

Feed health (AC13-16):
- `load_text` accepts only text whose first non-empty line is `BEGIN:VCALENDAR` and last is
  `END:VCALENDAR`, with balanced `BEGIN/END:VEVENT`. Otherwise raises `FeedError`; `events` and
  `last_ok` are kept, `last_error` set (plain sentence, no URL), alert (key `calendar/feed`). A valid
  calendar with zero events is accepted. `fetch_once` also refuses `Content-Type: text/html`.
- `Calendar.state()` → `unknown` (no successful fetch yet), `ok`, `stale` (no success for
  `calendar.stale_minutes`, default 60). Stale sends one alert; recovery sends one `recovered`.
- `door_state` gains `source` (`schedule`/`calendar`), `ends_at` (UTC epoch), `hand_alerted` (0/1)
  (AC16). With several active windows, `source = schedule` if any schedule window is active, else
  `calendar`; `ends_at` = latest end of the active windows.
- Engine rules: calendar `unknown` or `stale` → no new calendar window starts; a door held by a calendar
  window is not ended early; it ends at its recorded `ends_at` (never extended). Before the first fetch
  this means "left as it is" until its own recorded end (Decisions for Jake #5). Legacy 1.0.0 state rows
  (no `source`) are treated as `schedule` (no calendar is configured today).

Attribution (AC21): calendar unlocks log `user="calendar:<organizer>"` and the event title in `detail`
and in the alert.

### 3. Scheduler (`engine.py`)
- **UTC everywhere (AC19-20).** `schedule_occurrences` builds local times with `fold=0` (first
  occurrence on fall-back), converts to UTC, and checks existence by round trip (UTC → local must give
  back the requested wall clock). A start in the spring-forward gap is refused (spec Decision 4),
  logged and alerted (key `schedule/<id>/<date>`); an end in the gap is moved to the first valid minute
  after it (03:00) and documented. Same for calendar occurrences (RRULE `at()` and single events).
  `tick()` compares only UTC datetimes; `upcoming()` converts back to local for display. This fixes
  "unlocks twice" and "extra hour" (aware datetimes with the same tzinfo compare by wall clock today).
- **Single tick lock (AC22).** `self._tick_lock = threading.Lock()`. The loop blocks on it; `tick_soon`
  from the web tries `acquire(blocking=False)` and, if busy, sets `self._rerun` so one more tick runs
  right after. Never two ticks at once.
- **End action at end time (AC23).** For a door whose windows just ended, let `T` = the latest window
  end with `state.since < T <= now`; end action = `keep_lock` if any of that door's windows active just
  before `T` (`start < T <= end`) has `keep_lock`, else `reset`. Computed from the windows, not stored
  at start, so a later overlapping keep_lock window counts and a restart gives the same answer.
- **Per-door isolation (AC24).** Each door's handling is in `try/except Exception`; UniFi errors keep the
  retry path; any other exception is logged with traceback and alerted (key `door/<id>/error`), and the
  loop continues with the next door.
- **Hand changes (AC25, AC31).** At window end (existing check) and, during our window, every
  `hand_check_minutes` (default 5) for doors in state `unlock`: if the lock rule is no longer
  `keep_unlock`, log once, alert "changed by hand in UniFi (now: <rule>), left as is", set
  `hand_alerted=1`; the door is never touched again for that window and its end only clears our state.
- **Outage logging (AC27).** In-memory `_failing[(door, action)] = {count, first}`. First scheduler
  failure writes one log row; repeats only count; success writes one "worked again after N tries" row and
  sends `recovered`. State stays `unlock` on a failed end-of-window lock, so it is retried each tick.
  `door_action(..., quiet_repeat=True)` is passed by the scheduler only. After the first "cannot reach"
  in a tick, the remaining UniFi calls of that tick are skipped (retried next tick), so a down console
  cannot make one tick last minutes. A tick ended by a UniFi outage counts as **completed** (outage
  alert, not a stall).
- **Watchdog (AC26).** Thread `watchdog` checks every 10 s: stalled if no tick completed for
  `3 * tick_seconds` (90 s) or the scheduler thread is dead. A tick that raises an unexpected tick-level
  exception does not count as completed, so repeated failures also end up stalled. Stalled →
  `engine.healthy() == False`, one alert "scheduler stalled"; first completed tick after → `recovered`.
- `Engine(…, notifier=None)`; `now()` stays the single overridable clock (the existing `Clock` test
  subclass keeps working).

### 4. Health (AC26, AC43)
`/health`: still loopback only and refused with `CF-Connecting-IP`; body only `{"ok": bool, "version"}`;
HTTP 503 when `engine.healthy()` is false. Docker HEALTHCHECK (urllib raises on 503) then reports
unhealthy. Signed-in `/api/health` adds `calendar_state` (`off/unknown/ok/stale`) and alert counters.

### 5. Telegram (`telegram_bot.py`) (AC36-39)
- `Bot(cfg, engine, api=None, clock=time.time)`; `api` is injectable for the fake Telegram in tests.
- Offset (AC37): read `telegram_offset` from a new `kv` table at start; for each update, **store
  `update_id + 1` before handling it** (at-most-once: a crash never replays a door command).
- Age (AC37): messages with `date` older than 120 s (`telegram.max_age_seconds`) are dropped without
  action and without reply (INFO log with chat id and command name only).
- Group chats, channels, edited messages, `callback_query` (AC36): ignored, no reply (as today, now tested).
- Rate limit (AC38): per chat, at most `telegram.max_commands_per_minute` (default 10) in 60 s; the first
  extra gets one "Slow down: wait a minute." reply, later extras in that minute get nothing; no door action.
- Attribution (AC39): `handle(chat_id, text, name="")`; door actions pass
  `user="telegram:<chat_id>/<name>"`, name = `from.username` or `first_name`, control characters
  stripped, max 40 characters, `?` if missing. Never empty.

### 6. Access: server-side sessions and in-app people (`web.py`, `db.py`, `people.py`, `manage.py`)
How this sits with the pinned modules (no edits to them):
- **Sessions (AC40).** `web.py` stops using `Google.issue/verify/cookie_header` and `Auth.issue/verify`.
  After `Google.finish()` returns an email, or `Auth.check()` returns a name, `web.py` creates a random
  32-byte id, stores only `sha256(id)` in a new table `sessions(id_hash PK, user, kind google|admin,
  gen, created, expires, ip)` with `expires = now + 12 h`, and sets
  `__Host-doors_sid=<id>; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200`. Each request looks
  up the hash, checks expiry, and re-checks the user is still allowed (google: admin, in-app list or
  allowed domain; admin: account still in `accounts.json` with the same `gen`, so `set-password` and
  `remove-user` revoke). Logout deletes the row. Old `doors_auth` cookies are never read again
  (everyone signs in once more) and are cleared when seen. `SESSION_DAYS` goes; `SESSION_HOURS = 12`.
  Expired rows are purged hourly. `manage.py sessions` lists them (user, kind, expires);
  `manage.py revoke EMAIL|admin:NAME|--all` deletes them.
- **Who may sign in (AC41).** `web.DoorGoogle(google_signin.Google)`, a subclass defined in `web.py`.
  It overrides `emails` as a property: the setter (called by the parent `__init__`) stores the env
  admins; the getter returns admins plus the in-app list read from the DB. The parent's unchanged
  `finish()`, `ready()` and `start()` therefore accept admins and listed people, and still apply
  `GOOGLE_ALLOWED_DOMAINS` (empty at deploy, so no `hd` hint and no domain-wide access). Refusals are
  logged by `web.py` (log kind `signin`, ok=0) and printed by the module as today. A test pins the
  behaviour the subclass relies on; the hash-pin test keeps the module byte-identical.
- **People list (AC41a).** New table `people(email PK COLLATE NOCASE, added_by, added_at)`.
  Admin = Google session whose email is in `GOOGLE_ALLOWED_EMAILS` (break-glass `admin:NAME` is not an
  admin for this list; Decisions #2). Routes (all in `ROUTES`, CSRF on POST, `require_admin()` → 403 for
  everyone else): `GET /api/people` (env admins marked "admin, change in .env", plus the list),
  `POST /api/people` `{email}` (format check, max 254, lower-cased; duplicate or admin → plain error),
  `POST /api/people/remove` `{email, confirm}` (confirm must equal the email typed again; admins cannot
  be removed). Removal deletes that person's sessions in the same transaction. Add/remove → activity log
  (kind `people`, user = admin email) and alert (kind `people`). `bootstrap` returns `admin: true/false`;
  the People section is hidden and never loaded for non-admins.
- **Startup refusal (AC42).** `app.py` checks before creating `UnifiClient` or sending anything: host
  must be `https://` and (`cert_sha256` set or `verify_tls` true), else `sys.exit` with a plain message
  pointing to README "Pin the UniFi console certificate". Local development only:
  `DOORCONTROL_DEV_ALLOW_HTTP=1` **and** host `http://127.0.0.1` or `http://localhost` (never set in
  compose; the container's loopback cannot be the console). README development section updated.
- **Log retention (AC28).** `log` gets an additive column `kind` (`door`, `schedule`, `signin`, `people`,
  `system`; default `door`). The per-insert "keep 1000" delete goes. Hourly prune: rows older than
  `log_days` (default 90); sign-in rows additionally capped at the newest 5000. Sign-ins can no longer
  push door rows out.

### 7. Operations
- **Backups (AC44)** `doorcontrol/backup.py`: `run(data_dir)` writes
  `/data/backups/door-control-YYYYmmdd-HHMMSS.tar.gz` (mode 600, folder 700) holding a SQLite
  `backup()` copy of `doorcontrol.db`, `config.json`, `accounts.json`, `session-secret`; name collisions
  get `-2`, `-3`; keeps the 14 newest. Thread in `app.py`: runs at start if the newest backup is older
  than 24 h, then checks hourly. `manage.py backup` runs the same function and prints the path. A failed
  backup is logged and alerted (kind `backup`, deduped).
- **Logs (AC45)** compose `logging: driver json-file, options max-size 10m, max-file "5"`. README: the
  logs hold emails and IP addresses.
- **TZ (AC49)** Dockerfile `ENV TZ=America/New_York`; log timestamps become local; stored epochs
  unchanged.
- Deploy and rollback blocks rewritten (AC46-47); first-move note about sudo (AC48). See Deploy notes.

### 8. Front end (`static/app.js`, `index.html`, `style.css`)
- "Calendar stale" / "Calendar not read yet" notice from `/api/health` `calendar_state` (class-based, CSP-safe).
- People section (admins only): list, add form, remove through a dialog where the email must be typed
  (button disabled until it matches). Everything escaped; no inline script or style.
- Activity "By" shows `telegram:…` and `calendar:…` as given.

## Files to create or change
| File | Change | Why |
|------|--------|-----|
| `doorcontrol/notify.py` | New: queue, sender thread, dedupe, cap, scrub, switches | AC29-35 |
| `doorcontrol/people.py` | New: env admins, in-app list helpers, organizer allowlist | AC3, AC41, AC41a (no web import) |
| `doorcontrol/backup.py` | New: daily/on-demand tar backups, 14 kept | AC44 |
| `doorcontrol/ical.py` | Organizer/attendee/status parse, problems, RRULE validation and bound, nth weekday, EXDATE by day, feed validation, state/stale, all-day, cap | AC1-5, 9-18, 20 |
| `doorcontrol/engine.py` | `match_doors`, UTC windows and DST rules, tick lock, end action at end, per-door isolation, hand-change check, outage tracking, watchdog, schedule CRUD with user, alert hooks in `door_action` | AC6-8, 19-27, 30-31 |
| `doorcontrol/db.py` | `_migrate`: `door_state.source/ends_at/hand_alerted`, `log.kind`, tables `kv`, `sessions`, `people`; prune by age; connect `timeout=10` for manage.py | AC16, 28, 37, 40, 41a |
| `doorcontrol/telegram_bot.py` | Offset in DB, age drop, rate limit, attribution, injectable api/clock | AC36-39 |
| `doorcontrol/web.py` | Server-side sessions, `DoorGoogle` subclass, people routes + `require_admin`, `/health` 503, schedule routes call engine methods, `tick_soon` via the lock, `calendar_state` | AC22, 26, 40-41a, 43 |
| `doorcontrol/app.py` | Startup refusal, notifier wiring, watchdog and backup threads, startup alerts | AC3, 42, 44 |
| `doorcontrol/static/app.js`, `index.html`, `style.css` | Stale notice, People section, typed confirmation | AC15, 41a |
| `manage.py` | `backup`, `sessions`, `revoke` | AC40, 44 |
| `mock_unifi.py` | Modes: `hang` (sleep past the timeout), `fail_paths` (500 on chosen calls), hand-change helper, call log | AC22, 25, 27 tests |
| `Dockerfile` | `ENV TZ=America/New_York` (COPY unchanged: new modules are in `doorcontrol/`) | AC49 |
| `compose.yaml` | `image: door-control:1.1.0`, `logging:` block | AC45, 50 |
| `config.example.json` | `alerts{kinds, repeat_minutes, max_per_minute, queue_size, failures_before_alert}`, `calendar.{allowed_organizers, calendar_email, stale_minutes, max_window_hours}`, `telegram.{max_commands_per_minute, max_age_seconds}`, `hand_check_minutes`, `log_days`; `cert_sha256` shown | config |
| `.env.example` | `GOOGLE_ALLOWED_DOMAINS=` empty, `GOOGLE_ALLOWED_EMAILS=` admins (placeholders), comments | AC41 |
| `README.md` | Title 1.1.0; sign-in (12 h, people list, admins in .env), alerts, calendar rules (organizers, invitations off, exact names, no ALL, all-day/lookback, cap, DST rule, stale), backups, log privacy, startup refusal, dev escape hatch, sudo note | AC17, 45, 48, 50 |
| `CHANGELOG.md` | 1.1.0 entry | AC50 |
| `VERSION` | 1.1.0 | AC50 |
| `CLAUDE.md` | Current version, layout, new rules (notifier never blocks, sessions, DoorGoogle), deploy + rollback blocks, sudo note, History, Known DRIFT | AC47-48, 50 |
| `tests/test_all.py` | Existing engine/ical/telegram tests adapted (UTC, `handle` signature) + repo checks (version agreement, import graph, compose/Dockerfile/README/CLAUDE.md text) | AC45-50, 52 |
| `tests/test_safety.py` (new) | Calendar, DST, scheduler robustness, health, backups, startup refusal | A, B, F |
| `tests/test_alerts.py` (new) | Notifier with fake Telegram + fake clock; bot replay, offset, rate limit, groups | C, D |
| `tests/test_google.py` | ROUTES list, sessions, DoorGoogle, people, old cookies; pin unchanged | E |
| `tests/test_ui.cjs` | Stale notice, People section hidden/visible, typed confirm | AC15, 41a, 52 |

## Data / API / config / contract changes
- Schema (additive in `_migrate`): `door_state` + `source TEXT`, `ends_at REAL`,
  `hand_alerted INTEGER DEFAULT 0`; `log` + `kind TEXT NOT NULL DEFAULT 'door'`; new tables
  `kv(key PK, value)`, `sessions(...)`, `people(...)`. 1.0.0 code runs on the migrated DB unchanged (it
  names its columns).
- Routes: `GET /api/people`, `POST /api/people`, `POST /api/people/remove`. `/api/bootstrap` adds
  `admin`. `/api/health` adds `calendar_state` and alert counters. `/health` may return 503.
- Cookie: `__Host-doors_sid` replaces `doors_auth`; 12 h.
- config.json: new optional keys listed above, all with defaults. Behaviour change: an http, unpinned
  or unverified UniFi host now refuses to start.
- `.env`: no new variables; the values of `GOOGLE_ALLOWED_DOMAINS` / `GOOGLE_ALLOWED_EMAILS` change.
- No contract in `.claude/standards/contracts/` is touched.

## Cross-app impact
None. `google_signin.py` and `auth.py` are not edited (hash pin unchanged); no other app's data, API,
cookie or OAuth client changes (Door Control has its own OAuth client and Telegram bot). Integration
review is **not** required. Follow-up outside this PR (AC51): a separate dev-standards PR adding the Door
Control row and the "Shared code" mention to APPS.md.

DRIFT (both listed in the spec): no automatic backups (O1/R11, fixed here); APPS.md has no Door Control
row (O8/R12, separate dev-standards PR).

## Deploy notes
Version-bump files: `VERSION`, `compose.yaml` image tag, README title, `CHANGELOG.md`, "Current version"
in CLAUDE.md (a test checks they agree), plus CLAUDE.md History. No Dockerfile COPY change. Migrations
are additive and run at start; the deploy block backs up the volume first. Do not deploy within 15
minutes of a schedule or calendar window boundary, or during the fair.

What Jake does on fair-server, in order:
1. Precondition: 1.0.0 already runs in Docker. If the old systemd service still runs, do the first-move
   block in CLAUDE.md first (run `sudo -v` just before it so it does not stop at a password prompt, AC48).
2. **.env change (with the access PR, AC41).** One copy-paste block; keeps every other line and backs up
   the old file:
   ```bash
   (set -e
   cd /srv/fair-apps/door-control
   test -f .env || { echo "Stop: no .env here. Nothing changed."; exit 1; }
   umask 077
   mkdir -p -m 700 /srv/fair-apps/door-control-backups
   cp -p .env "/srv/fair-apps/door-control-backups/env-$(date +%Y%m%d-%H%M%S)"
   grep -v -e '^GOOGLE_ALLOWED_DOMAINS=' -e '^GOOGLE_ALLOWED_EMAILS=' .env > .env.new
   printf '%s\n' 'GOOGLE_ALLOWED_DOMAINS=' 'GOOGLE_ALLOWED_EMAILS=jake@hardincountyfair.org,kolt@hardincountyfair.org' >> .env.new
   chmod 600 .env.new && mv .env.new .env
   grep '^GOOGLE_ALLOWED_' .env)
   ```
   The deploy block below applies it (it recreates the container).
3. Run the new normal deploy block (AC47). Draft for the builder (final text in CLAUDE.md; `<…>` parts
   reuse the existing README backup and pin commands):
   ```bash
   (set -eE
   cd /srv/fair-apps/door-control
   V=$(docker version --format '{{.Server.Version}}'); if [ "${V%%.*}" -lt 28 ]; then echo "Stop: Docker $V is older than 28. Nothing changed. Tell Jake."; exit 1; fi
   if systemctl is-active --quiet door-control; then echo "Stop: the old systemd service runs. Use the first-move block. Nothing changed."; exit 1; fi
   test -n "$(docker compose ps --status running -q app)" || { echo "Stop: Door Control is not running. Nothing changed."; exit 1; }
   BKROOT=/srv/fair-apps/door-control-backups; mkdir -p -m 700 "$BKROOT"
   BK=$BKROOT/deploy-$(date +%Y%m%d-%H%M%S); mkdir -m 700 "$BK"; umask 077
   PREV=$(git rev-parse HEAD); echo "$PREV" > "$BK/previous-commit"
   docker compose run --rm -T --no-deps --user 0 -v "$BK":/out app sh -c '<sqlite backup of doorcontrol.db; copy config.json accounts.json session-secret; chmod 600>'
   test -f "$BK/doorcontrol.db" && test -f "$BK/config.json"
   STATE=$(python3 -c '<print http|pinned|verified|unpinned from the config>' "$BK/config.json")
   if [ "$STATE" = http ]; then echo "Stop: unifi.host is not https. Nothing changed."; exit 1; fi
   if [ "$STATE" = unpinned ]; then <openssl fingerprint as in README pin block; stop if empty; write config.json.new + mv via docker compose run>; fi
   git checkout -q main && git pull --ff-only
   docker compose build
   restore_prev() { trap - ERR; echo "Deploy FAILED: starting the previous version again."; git checkout -q "$PREV" || true; docker compose up -d || true; docker compose ps || true; echo "Backup in $BK"; }
   trap restore_prev ERR
   docker compose up -d
   S=starting; for i in $(seq 1 18); do sleep 5; S=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q app)"); [ "$S" = healthy ] && break; done
   if [ "$S" != healthy ]; then docker compose logs --tail=40; echo "Stop: not healthy ($S)."; restore_prev; exit 1; fi
   trap - ERR
   docker compose ps; echo "Deployed $(cat VERSION). Backup in $BK")
   ```
   `restore_prev` uses `docker compose up -d` without `--build`: the previous commit's compose.yaml
   names the old image tag (`door-control:1.0.0`), which is still on the server. The pin is written
   before the new container starts, so doors are never unmanaged by a refused start (AC42).
4. After the deploy that carries the access changes: everyone (Jake, Kolt, break-glass) signs in again
   once (old 14-day cookies stop working). Jake adds other staff in Settings › People.
5. Telegram: send the bot `/doors`; the next door status change produces an alert. Alerts go only to the
   private chats in `allowed_chat_ids`.
6. Manual checks (listed in CLAUDE.md): `/health` 200 inside the container; a hardincountyfair.org
   account not on the list is refused; People add/remove works and removal signs that person out;
   "Unlock 1 hr" on a test door gives a Telegram alert with your email;
   `docker compose exec app python manage.py backup` prints a path.

No new `.env` variables. No Cloudflare change. No calendar is configured; when one is added later the
README says: dedicated calendar, invitations off, its organizer address in
`calendar.allowed_organizers` (Decisions #4).

## Risks
| Risk | Mitigation |
|------|------------|
| App refuses to start after upgrade because the certificate is not pinned | Deploy block pins **before** the restart; stops with nothing changed if it cannot read the fingerprint or the host is http; `restore_prev` on unhealthy. |
| Alerts slow or break door control | Enqueue is O(1) and never raises; sending on its own thread with timeouts; test shows tick duration with Telegram hanging equals without. |
| Alert flood (UniFi outage, many doors) | Per-key dedupe (30 min), 20/min cap with one summary, outage/recovered pairs. |
| Subclassing the pinned `google_signin.Google` breaks if Awards' module changes | Pin test blocks any silent change; a dedicated test covers the `emails`/`domains`/`finish` behaviour relied on; CLAUDE.md "Shared code" notes it. |
| Lock-out after the .env change | Break-glass password still works (same sessions table); `.env` backed up by the block; Jake and Kolt are admins. |
| DST rule surprises (a 02:30 schedule refused on one day) | Alert names the schedule and date; README documents it; tests for both transitions. |
| Calendar rules block real events | Each refusal is alerted with its reason; no feed configured today; README setup steps. |
| Hung UniFi calls stretch a tick and trip the watchdog | 10 s timeout; remaining UniFi calls skipped after the first "cannot reach" in a tick; a handled outage counts as a completed tick. |
| Extra UniFi reads for hand-change detection | Only for doors we hold unlocked, every 5 min. |
| manage.py and the app write the DB at the same time | `timeout=10`; writes are short. |
| Backups hold tokens | Files 600, folder 700, inside the volume only; README says so. |
| Size of the change makes review hard | Two PRs (below). |

## Test plan
Tools: `mock_unifi.py` (plus `hang`, `fail_paths`, hand-change helper, call log), a fake Telegram
(`api=` callable recording calls, scripted `getUpdates`, can raise or sleep), fake clocks (`Engine.now`
override, `Notifier(clock=)`, `Bot(clock=)`), and the real web server from `test_google.py` with Google's
token endpoint faked.

| Acceptance criterion | Test |
|----------------------|------|
| AC1 | test_safety `CalendarAccept.test_status_tentative_cancelled_missing_make_no_window` |
| AC2 | `test_organizer_not_allowed_or_missing_refused_logged_alerted_once` (one alert over 3 refreshes) |
| AC3 | `test_allowlist_default_admins_plus_people`, `test_config_allowlist_overrides`, `test_both_empty_no_window_one_startup_alert` |
| AC4 | `test_invitation_needs_action_or_tentative_refused_even_if_organizer_allowed` (with and without `calendar_email`) |
| AC5 | `test_all_and_star_open_nothing_and_alert` |
| AC6 | `MatchDoors.test_full_name_not_split` ("Pole Barn and Arena" as a test door) |
| AC7 | `test_split_parts_exact_then_single_partial` |
| AC8 | `test_part_none_or_several_skipped_other_parts_apply_alert_names_part` ("Main" vs Main Gate/Main Office) |
| AC9 | `Rrule.test_bad_parts_skipped` (BYMONTHDAY 0/32/40, COUNT/INTERVAL 0, -1, x, unknown FREQ, unknown TZID, bad UNTIL), each under a 2 s timeout guard; schedules still apply in the same tick |
| AC10 | `test_exception_in_one_event_only_drops_it` (patched expansion raises) |
| AC11 | `test_monthly_nth_and_last_weekday` (2TU, 1MO, -1FR, BYDAY+BYSETPOS), `test_unsupported_byday_skipped_alerted` (5MO, WEEKLY 1MO) |
| AC12 | `test_exdate_value_date_cancels_timed_occurrence` |
| AC13 | `Feed.test_empty_html_truncated_missing_end_keep_last_events_set_error_alert` (mock door stays unlocked) |
| AC14 | `test_before_first_fetch_calendar_door_left_and_no_new_window` |
| AC15 | `test_stale_after_60_min_one_alert_no_new_windows_open_ends_on_time`; test_ui "calendar stale" notice |
| AC16 | `test_state_records_source_and_ends_at`; migration test on a 1.0.0-shaped DB |
| AC17 | `test_all_day_midnight_to_midnight`; README text check |
| AC18 | `test_over_cap_refused_buffers_counted`, `test_all_day_on_dst_day_accepted`, `test_multiday_all_day_refused` |
| AC19 | `Dst.test_fall_back_0130_0145_once` (ticks every 5 min from 01:00 EDT to 02:30 EST on 2027-11-07: one unlock, one reset), `test_end_0200_no_extra_hour`; same for a calendar event |
| AC20 | `Dst.test_spring_forward_start_in_gap_refused_alerted_never_twice` for a schedule and a calendar event (2027-03-14); end in gap moved to 03:00 |
| AC21 | `test_calendar_unlock_logged_as_calendar_organizer_with_title` |
| AC22 | `Scheduler.test_two_ticks_never_overlap` (mock `hang` 1 s, `tick_soon` during a tick: one PUT only, rerun afterwards) |
| AC23 | `test_overlap_keep_lock_started_later_wins_at_end`, `test_end_action_same_after_restart` |
| AC24 | `test_non_unifi_error_on_one_door_others_processed_and_alerted` |
| AC25 | `test_hand_change_left_alone_logged_once_alerted_once` (during the window and at its end) |
| AC26 | `Watchdog.test_hung_tick_gives_503_and_one_alert_then_recovered`, `test_dead_thread_503`; HTTP `/health` 503 via the real server; Dockerfile HEALTHCHECK text check |
| AC27 | `test_unifi_down_one_row_per_door_action_plus_recovery`, `test_timeout_end_lock_retried` (mock `hang` longer than the timeout), `test_500_on_end_lock_state_stays_unlock_and_retried` |
| AC28 | `Log.test_signins_do_not_push_out_door_rows`, `test_prune_by_age_90_days` |
| AC29 | test_alerts `test_error_alerts_plain_text_no_token_no_url` (door failure, tick error, stall, feed failure, stale; scrub checked) |
| AC30 | test_google `test_schedule_create_edit_delete_pause_resume_alert_with_email` |
| AC31 | `test_status_alert_from_web_telegram_schedule_calendar_with_source_and_end`; hand-change alert (AC25 test) |
| AC32 | `Notifier.test_telegram_down_tick_duration_unchanged` (fake send sleeps 5 s / raises), `test_full_queue_drops_oldest_low_and_counts`, `test_high_drop_logged` |
| AC33 | `test_dedupe_then_repeat_summary_after_30_min`, `test_cap_20_per_minute_then_one_summary` |
| AC34 | `test_one_recovered_per_outage` (unifi, calendar, scheduler) |
| AC35 | `test_no_token_or_no_chats_logs_only_never_raises` |
| AC36 | `Bot.test_group_chat_and_callback_ignored_no_reply`, `test_alerts_skip_negative_chat_ids` |
| AC37 | `Bot.test_old_message_dropped_no_reply`, `test_restart_reads_offset_old_unlock_not_run` (new Bot on the same DB, fake getUpdates replays) |
| AC38 | `Bot.test_rate_limit_one_slow_down_no_action` |
| AC39 | `Bot.test_door_action_logged_as_telegram_chat_name` (and `?` without a name) |
| AC40 | test_google `Sessions.test_db_stores_hash_only`, `test_expires_after_12h`, `test_manage_revoke`, `test_removed_from_env_refused_after_restart`, `test_old_doors_auth_cookie_refused`, `test_set_password_revokes_admin_session`, `test_logout_deletes_row` |
| AC41 | `test_only_admins_and_people_sign_in_domain_account_refused_and_logged`, `test_all_empty_no_google_breakglass_works`, existing pin test |
| AC41a | `People.test_admin_add_validates_format_case_duplicates`, `test_remove_needs_typed_confirm`, `test_non_admin_403`, `test_admin_cannot_be_removed`, `test_remove_ends_sessions_at_once`, `test_add_remove_logged_and_alerted`; test_ui section hidden for non-admins, confirm button |
| AC42 | test_safety `Startup.test_http_host_refused_no_request_sent` (mock logs zero calls), `test_unpinned_unverified_refused`, `test_pinned_ok`, `test_dev_escape_only_loopback_http`; CLAUDE.md deploy block pins before `up -d` (text check) |
| AC43 | test_google: `/health` from non-loopback 404, with CF header 404, body only ok + version |
| AC44 | `Backup.test_daily_file_600_14_kept_restores_working_db` (extract, `integrity_check`, schedules and files present), `test_manage_backup` |
| AC45 | repo test: compose.yaml `logging:` json-file with max-size and max-file; README mentions emails and IPs in logs |
| AC46 | repo test: rollback block restores config.json, accounts.json, session-secret, checks systemd and newer data before `docker compose stop`; `bash -n` on the extracted block; reviewer dry run on a scratch volume |
| AC47 | repo test: deploy block order (Docker check, previous commit, backup, pin, pull, build, up, healthy wait, restore) in a single `( set -eE … )`; `bash -n` |
| AC48 | repo test: README and CLAUDE.md mention the sudo password prompt for the first-move block |
| AC49 | repo test: Dockerfile has `ENV TZ=America/New_York` |
| AC50 | `Repo.test_version_agrees` (VERSION, compose image, README title, CHANGELOG heading, CLAUDE.md) |
| AC51 | PR checklist: separate dev-standards PR linked; nothing under `.claude/standards/` changed |
| AC52 | All of the above; existing tests, `black --check .` and the prettier check from CLAUDE.md pass |

## Rollback
- Automatic: the deploy block restores the previous commit and its image when the new version is not
  healthy within 90 s.
- By hand later (new rollback block in CLAUDE.md, AC46): asks for the `deploy-…` backup folder; **before
  stopping anything** checks that the folder has `doorcontrol.db`, `config.json` and `previous-commit`,
  that the old systemd service is not active, and that the volume has no activity newer than the backup
  (newest `log.ts` and schedule count, read through a read-only `docker compose run`); if newer data
  exists it stops unless the person types `REPLACE NEWER DATA`. A `restored` marker in the folder makes a
  second run stop. Then it saves the current volume to `pre-rollback-<time>` (same layout), stops the
  container, copies `doorcontrol.db`, `config.json`, `accounts.json`, `session-secret` back (owner 10001,
  mode 600), checks out `previous-commit`, runs `docker compose up -d`, waits for healthy; any failure
  after the stop starts the 1.1.0 version again (`restore_new`). The marker is written last.
- Data compatibility: migrations are additive, so 1.0.0 also runs on the 1.1.0 database. After a
  rollback, sessions go back to 14-day stateless cookies (everyone signs in again) and
  `GOOGLE_ALLOWED_*` stay as set (only Jake and Kolt can sign in under 1.0.0); restore
  `door-control-backups/env-*` if others need access.
- Never `docker compose down -v`.

## PR split (recommended)
- **PR 1 `feature/safety-alerts` → 1.1.0**: A (AC1-21; AC3 default is admins only until PR 2), B
  (AC22-28), C (AC29-36), D (AC37-39), AC42-50 and the AC52 tests for these. Fixes the three BLOCKERs
  and adds alerting first. No `.env` change; sign-in unchanged.
- **PR 2 `feature/access-tightening` → 1.2.0**: E (AC40, AC41, AC41a), AC3 extended with the in-app
  list, session and people tests, the `.env` block, "everyone signs in again", version bump files.
- AC51: separate dev-standards PR after PR 1 deploys.
If Jake prefers one PR, everything above ships as 1.1.0 unchanged.

## Decisions for Jake (defaults in bold)
1. Split into two PRs (1.1.0 safety/alerts/ops, 1.2.0 access): **yes**. This changes AC50's single 1.1.0.
2. Break-glass `admin:NAME` may manage the People list: **no** (spec: admins = `GOOGLE_ALLOWED_EMAILS`).
3. Invitation rule when `calendar.calendar_email` is unset: **any non-organizer attendee with NEEDS-ACTION/TENTATIVE refuses the event**.
4. Google secondary calendars list the calendar's own address as ORGANIZER (not the staff member): **when a feed is added, put that address in `calendar.allowed_organizers`; attribution then shows the calendar address**.
5. Calendar-held door while the feed is unknown at startup: **left as is until its recorded end time, then ended** (not left open forever).
6. Window end inside the spring-forward gap: **moved to 03:00**.
7. Multi-day all-day events: **refused with an alert (one event per day)**.
8. Hand-change check during our windows: **every 5 minutes**.
9. The 20/minute cap counts alerts (one alert to all chats = 1): **yes**.
