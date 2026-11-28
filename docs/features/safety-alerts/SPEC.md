# Spec: Safety fixes and Telegram alerts (Door Control 1.0.0 to 1.1.0)

Size: Large. Branch: feature/safety-alerts. Source: audit /tmp/audit-dc.md (2026-11-27, verdict NO-GO), plus Jake's request to push errors, schedule updates and status changes to Telegram.

## Problem
The audit found that a calendar feed can unlock doors from outsider invitations, that a malformed recurrence rule or an empty feed can stop the scheduler or lock doors mid-event, that old Telegram commands replay after a restart, that sessions last 14 days for every domain account, and that nobody is told when something fails. Deploy, rollback and backup steps are also incomplete. Door Control has no calendar configured today, but it must be safe whenever a feed is added (Jake: it would be an internal calendar only fair staff add dates to).

## Goal
Every BLOCKER, SHOULD-FIX and NIT in the audit is fixed and tested, and every error, schedule change and door status change reaches Telegram without ever delaying or breaking door control. Fail-safe rule everywhere: never guess; on doubt keep the current state and alert; a door changed by hand is never touched.

## Definitions
- Door-affecting event: a calendar event whose title starts with the prefix ("Unlock:").
- Alert: a Telegram message to every chat in `allowed_chat_ids`. Telegram chat id and name are stored as `telegram:<chat_id>/<name>`.
- Window: a period a schedule or calendar event holds a door unlocked.

## Acceptance criteria

### A. Calendar safety (S2, S1/R2, Q1/R1, Q2/R3, Q3/R8, R4, S3, R9, S5 calendar part)
Event acceptance (S2)
- [ ] AC1: Given an event with STATUS other than CONFIRMED (TENTATIVE, CANCELLED, missing), when windows are built, then it creates no window. A missing STATUS is treated as not confirmed.
- [ ] AC2: Given an event whose ORGANIZER email is not in the organizer allowlist, or has no ORGANIZER, when windows are built, then it creates no window, the refusal is logged with the organizer and title, and an alert is sent (deduplicated).
- [ ] AC3: Given config `calendar.allowed_organizers` is absent, then the allowlist defaults to the emails allowed to sign in (admins in `GOOGLE_ALLOWED_EMAILS` plus the in-app list, AC41a). Given it is set, then only those emails count. If both are empty, no calendar event creates a window and one startup alert says why.
- [ ] AC4: Given an invitation (event where the organizer is someone else and our attendee PARTSTAT is NEEDS-ACTION or TENTATIVE), then it never creates a window, even if the organizer is allowlisted.
- [ ] AC5: Given the name "ALL" (any case) or "*" in an event, then it opens nothing, is logged, and an alert is sent. The calendar never opens every door.

Door matching (Q2/R3)
- [ ] AC6: Given the text after the prefix equals a full door name (name or full_name, case-insensitive), then that one door is used and the text is not split (so "Pole Barn and Arena" works if that is a door name).
- [ ] AC7: Given the text is not a door name, when split on commas, "&" and "and", then each part must match exactly one door: exact name first, else a partial match (Jake's rule) that matches exactly one door.
- [ ] AC8: Given a part matches no door or more than one door ("Main" vs "Main Gate" and "Main Office"), then no door is opened for that part, the event is logged with the reason, and an alert names the event and part. Other valid parts of the same event still apply.

Recurrence (S1/R2, R4)
- [ ] AC9: Given RRULE parts that are malformed or out of range (BYMONTHDAY 0, 32 or 40, COUNT or INTERVAL not a positive integer, unknown FREQ, unknown TZID), then that event is skipped, logged and alerted, and expansion ends within a fixed candidate limit. The tick completes and other events and all schedules are processed.
- [ ] AC10: Given one event raises any exception while expanding, then only that event is dropped; the calendar error never stops schedule processing.
- [ ] AC11: Given MONTHLY with BYDAY/BYSETPOS ("2nd Tuesday", 1MO, -1FR) or BYDAY with numeric prefixes in WEEKLY, then it is either expanded correctly or the event is skipped, logged and alerted. It is never expanded as a different pattern. (Builder chooses; tests cover whichever is chosen. Default: support "nth weekday" monthly and "last weekday"; skip the rest.)
- [ ] AC12: Given EXDATE with VALUE=DATE and a timed occurrence on that day, then that occurrence is cancelled (EXDATE compared by local day for date-only values).

Feed health (Q1/R1, R9)
- [ ] AC13: Given a feed reply that is empty, HTML, truncated or lacks a complete BEGIN:VCALENDAR ... END:VCALENDAR, then the last good events are kept, `last_error` is set, an alert is sent, and no calendar window ends early.
- [ ] AC14: Given the first fetch has not succeeded yet (startup), then calendar windows are unknown: doors unlocked by a calendar window (recorded window source "calendar") are left as they are, not locked, and no calendar window is started.
- [ ] AC15: Given the feed has not been fetched successfully for more than `calendar.stale_minutes` (default 60), then the dashboard shows "calendar stale", an alert is sent once, and new calendar windows do not start. Already open calendar windows end on their own end time (not extended).
- [ ] AC16: Given state records the window source (schedule or calendar) per door, then end-of-window logic and the stale rule use it. (additive column)
- [ ] AC17: Given an all-day event, then it is a window from local midnight to midnight and README documents this and the lookback behavior (R9).

Window cap (S3)
- [ ] AC18: Given an event longer than `calendar.max_window_hours` (default 24), then it is refused, logged and alerted. An all-day event of 24 h works. Cap counts the buffers.

DST (Q3/R8)
- [ ] AC19: Given the clocks fall back on 2027-11-07, then a 01:30-01:45 window unlocks and locks exactly once, and a window ending 02:00 ends at the right moment (no extra hour). Comparisons are done in UTC.
- [ ] AC20: Given the clocks spring forward on 2027-03-14, then a schedule or event starting in the missing hour (02:00-02:59) is refused or moved by a documented rule, logged, and alerted; it never fires twice. Tests cover both transitions for schedules and calendar events.

Attribution (S5 calendar)
- [ ] AC21: Given a door is unlocked by a calendar event, then the activity log user is `calendar:<organizer email>` and the event title is recorded.

### B. Scheduler robustness (R6, Q4/R7, O4/R10, O3, Q5)
- [ ] AC22: Given the web `tick_soon` and the loop fire together, then only one tick runs at a time (single tick lock); a second caller waits or skips. Test shows no double UniFi call.
- [ ] AC23: Given a window ends and another window (same door) with end_action keep_lock is still active or started later, then the end action is chosen when the window ends, from the windows active at that moment (Q4/R7).
- [ ] AC24: Given any non-UniFi exception while handling one door, then the other doors in the tick are still processed, and the error is logged and alerted.
- [ ] AC25: Given a door was changed by hand (rule no longer what we set), then the app does not touch it, logs it once, and sends a status-change alert "changed by hand" (see D).
- [ ] AC26: Given the last completed tick is older than 3 intervals (90 s), then `/health` returns 503, the Docker HEALTHCHECK reports unhealthy, and an alert "scheduler stalled" is sent once. Given ticks resume, a "recovered" alert is sent. A watchdog thread detects a tick that hangs (not just a tick that raised).
- [ ] AC27: Given UniFi is down, then repeated retries write one activity-log row per door and action per outage (plus a final recovery row), not one every 30 s (Q5). Tests cover UniFi timeouts and a 500 on the end-of-window lock (the door stays "unlock" in state and is retried).
- [ ] AC28: Given the activity log keeps rows by age (default 90 days) and sign-in rows are kept separately from door actions, then sign-ins no longer push door actions out of the log (S9).

### C. Telegram alerts (O3 and Jake's request)
Messages to every allowed chat:
- [ ] AC29 (errors): Given a door action fails after its retries, a scheduler tick error or stall, a calendar fetch or parse problem, or the calendar becoming stale, then an alert is sent with plain wording (what, which door/event, what the app is doing about it). Alerts never include tokens or the calendar URL.
- [ ] AC30 (schedule updates): Given any schedule is created, edited, deleted, enabled or disabled from the web or Telegram, then an alert says what changed and who did it (web user email or `telegram:<chat>/<name>`).
- [ ] AC31 (status changes): Given any door lock, unlock or back-to-normal from any source (web user, Telegram user, schedule, calendar), then an alert says door, new state, source, who/what, and the end time when known. Given a door's state changed by hand in UniFi and the app detects it (rule or relay differs from what it set, found while reconciling), then an alert says so. Detection is best effort; the app only reports and never "fixes" it.
- [ ] AC32 (never blocks): Given Telegram is slow, down or returns errors, then door actions and ticks take no longer than without Telegram: notifications go through a bounded in-memory queue sent by a separate thread; a full queue drops the oldest low-priority messages (status changes) and counts them; errors are never dropped silently without a log line. A Telegram outage test shows tick duration unchanged.
- [ ] AC33 (rate limit and dedupe): Given the same alert key (kind + door + reason) repeats, then it is sent once, then at most once per `alerts.repeat_minutes` (default 30) with a summary "repeated N times since ...". Total sends are capped per minute (default 20, below Telegram's limits); extras are batched into one summary message.
- [ ] AC34: Given an outage ends, then one "recovered" message is sent (UniFi, calendar, scheduler).
- [ ] AC35: Given the bot token is empty or no chats are allowed, then alerts are only logged and nothing fails.
- [ ] AC36: Given the bot is used in a group chat or by callbacks (Q6), then they are ignored, no reply is sent, and a test covers it. Alerts go to private allowed chats only.

### D. Telegram access (S4/R5, S5 Telegram part)
- [ ] AC37 (S4): Given a queued message older than 2 minutes, then it is dropped without action (and without reply). Given a restart, then the update offset is read from the database so earlier updates are not replayed. Test: an old `/unlock` is not run after restart.
- [ ] AC38 (S4): Given a chat sends more than 10 commands in a minute (config), then the rest get one "slow down" reply and no door action.
- [ ] AC39 (S5): Given a door action from Telegram, then the log user is `telegram:<chat_id>/<name>` (never empty).

### E. Access tightening (S6, S7, S8)
- [ ] AC40 (S6): Given a Google or password sign-in, then a server-side session is stored (only a hash of the id), lasts 12 hours, and can be revoked. Given a user is removed from the allowed list and the app restarts, or the session is revoked with `manage.py`, then the next request is refused at once. Old 14-day stateless cookies stop working after the upgrade (everyone signs in again once).
- [ ] AC41 (S7): Given `GOOGLE_ALLOWED_EMAILS` lists the admins (jake@hardincountyfair.org, kolt@hardincountyfair.org at deploy) and `GOOGLE_ALLOWED_DOMAINS` is empty, then only admins and people on the in-app list can sign in; any other account, including other hardincountyfair.org accounts, is refused and logged. Given both are empty and the in-app list is empty, nobody signs in with Google (break-glass password still works).
- [ ] AC41a: Given an admin (an email in `GOOGLE_ALLOWED_EMAILS`) opens Settings › People, then they can add an email (checked format, case-insensitive, no duplicates) and remove one, with a typed confirmation for removal. The in-app list is stored in the database (additive table). People on the in-app list can use the dashboard but cannot see or change the list; admins cannot be removed in the app (only in `.env`). Removing a person ends their sessions at once. Every add/remove is logged with who did it and sent as a Telegram alert. Calendar organizers (AC3) default to admins plus the in-app list.
- [ ] AC42 (S8): Given the UniFi host is not https, or there is no `cert_sha256` pin and `verify_tls` is not true, then the app refuses to start with a plain message and sends no token. Given an unpinned install after upgrade, then the deploy block pins before the restart (see F) so doors never go unmanaged.
- [ ] AC43: Given `/health` is called, then it is still loopback only and shows no sensitive details.

### F. Operations (O1/R11, O2, O5, O6, O7/S10, O8/R12, O9, O10, R13)
- [ ] AC44 (O1/R11): Given each day, then a backup of the database, config.json (tokens), accounts and session secret is written to `/data/backups` (permissions 600), 14 kept; `manage.py backup` does the same on demand. Test: backups restore to a working database.
- [ ] AC45 (O2): Given compose.yaml, then it has a `logging:` block (json-file, max-size and max-file set). README says the logs hold emails and IPs.
- [ ] AC46 (O5, O6): Given the rollback block, then it also restores `/data/config.json` and the accounts and session files from the backup; it checks the old systemd service is not running and refuses if the new data is newer than the backup unless told; running it twice does not overwrite newer data. Everything is checked before the container is stopped.
- [ ] AC47 (O7/S10): Given the normal deploy block, then it (in order): checks Docker >= 28 and stops if not; records the previous commit (file in a backups folder); backs up the data volume; pins the UniFi certificate if `cert_sha256` is unset, before restarting; pulls and builds; starts; waits up to 90 s for healthy; on any failure after the stop, restores the previous commit and starts it. Block is a single copy-paste `( set -e ... )`.
- [ ] AC48 (O9): README and CLAUDE.md say the first-move block uses sudo and may stop at a password prompt (run it so sudo is already unlocked).
- [ ] AC49 (O10): Given the Dockerfile sets `ENV TZ=America/New_York`, then container logs show local time. Existing UTC storage is unchanged.
- [ ] AC50 (R13): Version 1.1.0 is in `VERSION`, compose.yaml `image: door-control:1.1.0`, README title, a new `CHANGELOG.md` entry, and "Current version" in CLAUDE.md; a test (or the checklist in the PR) checks they agree. CLAUDE.md History and "Known DRIFT" are updated.
- [ ] AC51 (O8/R12, DRIFT): A separate dev-standards PR adds the Door Control row (and "Shared code") to APPS.md. This spec's PR does not edit dev-standards.

### G. Tests (every audit "untested scenario")
- [ ] AC52: New tests cover Q1 (empty/HTML/truncated feed, startup before first fetch), Q2 (partial matches: none, one, several, "Pole Barn and Arena"), Q3 (both DST transitions), overlap windows, a CANCELLED event, UniFi timeouts and a 500 on the end lock, malformed RRULEs (BYMONTHDAY 0/32/40, bad COUNT/INTERVAL, unknown FREQ and TZID), Telegram replay (old message, restart offset), group chats/callbacks, notification queue behavior with Telegram down, dedupe/summary, sessions (expiry, revoke), and startup refusal without https/pin. Existing tests, black and prettier stay clean. Node UI tests cover the "calendar stale" notice.

## Audit ID map
S2 AC1-5. S1/R2 AC9-10. Q1/R1 AC13-14,16. Q2/R3 AC6-8. Q3/R8 AC19-20. R4 AC11-12. R6 AC22. Q4/R7 AC23-24. S3 AC18. R9 AC15,17. S4/R5 AC37-38. S5 AC21,39. S6 AC40. S7 AC41, AC41a. S8 AC42. O3 AC29-35. O4/R10 AC26. O1/R11 AC44. O2 AC45. O5/O6 AC46. O7/S10 AC47. O8/R12 AC51. S9 AC28. Q5 AC27. Q6 AC36. O9 AC48. O10 AC49. R13 AC50.

## Out of scope
- Reading any calendar by default; no feed is configured today (README documents setup of a dedicated internal calendar with invitations off).
- Editing the standards or APPS.md here (separate dev-standards PR).
- Changing the shared `google_signin.py` and `auth.py` copies (pinned by hash; any fix comes from Awards).
- Two-way Telegram schedule editing, new bot commands, group chats, other messaging apps.
- Fixing doors changed by hand (only reported).
- Full RFC 5545 support beyond what AC9-12 name.

## Other apps affected
- dev-standards: APPS.md row (separate PR). No data or API link to any other app.
- Shares the Cloudflare tunnel and OAuth client with Awards, Budget, Thank You, Incident Report; no change to them. Raising `GOOGLE_ALLOWED_EMAILS` here is this app's `.env` only.
- Telegram bot token is this app's own.

## Decisions (Jake, 2026-11-28)
1. Admins: Jake and Kolt in `GOOGLE_ALLOWED_EMAILS`; more people added in the app by an admin (AC41a). Domain list empty.
2. Alerts go to every allowed Telegram chat.
3. All alert kinds on by default, with a config switch per kind.
4. A spring-forward start in the missing hour is refused, logged and alerted.
5. Monthly nth/last weekday supported; other unsupported rules skipped with an alert.
6. Same alert repeats at most every 30 min; at most 20 messages a minute.
7. Daily backups in `/data/backups` (14 kept); deploy-block backups in `/srv/fair-apps/door-control-backups`.

## Open questions
None.
