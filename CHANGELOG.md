# Changelog

## 1.1.0 (2026-11-28)
Safety fixes from the 2026-11-27 audit, and Telegram alerts.
- **Calendar safety.** A calendar event opens doors only if it is CONFIRMED, its organizer is allowed
  (`calendar.allowed_organizers`, else the emails in `GOOGLE_ALLOWED_EMAILS`), it is not an unanswered
  invitation, and it is at most `calendar.max_window_hours` (24) long including buffers. "ALL" and "*"
  never open anything. Door names must match exactly one door (a whole door name is never split);
  unclear parts are skipped and alerted. Repeat rules are checked and bounded (unsupported or
  malformed ones are skipped and alerted, never looping); monthly "2nd Tuesday" / "last Friday" are
  supported; a date-only EXDATE cancels that day. An empty, HTML or cut-off feed keeps the last good
  calendar. Before the first read and when the feed is stale (60 min), no calendar unlock starts and
  doors already open for an event close at their recorded end. Calendar unlocks are logged as
  `calendar:<organizer>` with the event title.
- **Scheduler.** All times compared in UTC: the fall-back hour no longer unlocks twice or stays open an
  extra hour; a start in the spring-forward gap is skipped and alerted, an end there moves to 03:00.
  One tick at a time; the end action is chosen when the window ends; an error on one door never
  stops the others; doors changed by hand in UniFi are reported (checked every 5 minutes) and left
  alone; one activity row per door and action per UniFi outage; a watchdog makes `/health` answer 503
  (Docker "unhealthy") when no tick finished for 90 s.
- **Telegram alerts** to every allowed private chat: door failures, scheduler and calendar problems,
  backups, schedule changes and every door status change (who, source, until when), with "recovered"
  messages. Sent from a background queue, so Telegram never slows door control; the same alert repeats
  at most every 30 minutes; at most 20 a minute. Every kind can be switched off in `alerts.kinds`.
- **Telegram access.** The update offset is kept in the database (no replay after a restart); messages
  older than 2 minutes are dropped; 10 commands a minute per chat; group chats and buttons ignored;
  actions logged as `telegram:<chat id>/<name>`.
- **Startup refusal.** Door Control refuses to start, sending nothing, unless `unifi.host` is https and
  `unifi.cert_sha256` is set (or `verify_tls` is true). The new deploy block pins the certificate
  before the restart.
- **Operations.** Daily backups in `/data/backups` (database, config.json, accounts.json,
  session-secret; 14 kept, mode 600) and `manage.py backup`; activity log kept by age (90 days), with
  sign-ins capped separately; Docker log rotation; container logs in local time; new deploy block
  (Docker check, backup, pin, healthy wait, automatic return to the previous version) and rollback
  block (restores config and sign-in files too, refuses to overwrite newer data twice).
- **Review round 1.** When a schedule and a calendar event overlap on a door, a stale or not-yet-read
  calendar never shortens the event's hold (its end is kept separately). An empty or unclear answer from
  UniFi is never taken as a change by hand: it is retried and alerted ("could not read the door"), and a
  failed read at a window's end writes one activity row per outage. Calendar events back to back may
  not keep a door unlocked for more than 24 hours without a break, and `max_window_hours` is capped at
  24; buffers no longer make all-day events too long. Every calendar event that opens a door is alerted
  with its title and UID. Links are removed from event titles in alerts. Chats that are not allowed are
  answered before the command limit (at most once a minute). An alert at startup when
  `DOORCONTROL_DEV_ALLOW_HTTP` is set. A feed starting with a byte order mark is read. The deploy block
  also runs when the app is down, the automatic return waits for healthy, and only the newest 10 deploy
  and rollback folders are kept; the rollback block checks the typed folder name.
- Additive migration: `door_state.source/ends_at/hand_alerted/cal_ends_at`, `log.kind`, table `kv`. No
  new `.env` lines. New optional `config.json` keys (see `config.example.json`; `allowed_organizers` is
  not in it on purpose, see README).

## 1.0.0 (2026-11-27)
First numbered release.
- Google sign-in (verified hardincountyfair.org accounts) at https://doors.jakemc.me replaces the
  shared dashboard password. The fair network needs sign-in too. Break-glass admin password from
  `manage.py set-password`, rate limited, shown only when an account exists.
- Activity log records who did it: email on door actions, schedule changes (now logged) and sign-ins.
  Additive database migration (`log.user`).
- Runs in Docker Compose (read-only container, non-root, volume `door_control_data`), replacing the
  systemd service (kept only for rollback).
- Security: strict CSP and security headers on every response, CSRF token on every change, Cloudflare
  traffic for other hostnames refused, plain error messages.
- Code formatted with black and prettier; JS and CSS moved out of `index.html`.
- Review round 3: the port is published on 127.0.0.1 only; the dashboard is reachable only through
  https://doors.jakemc.me (Cloudflare tunnel URL `127.0.0.1:8480`). Pin block handles IPv6 consoles.
- Review round 2: the app gets its own Docker network `door-control-net` (172.31.248.0/29) and only
  its gateway (172.31.248.1, where cloudflared on fair-server appears) is trusted; deploy steps pin the
  UniFi console certificate.
- Review round 1: Cloudflare headers and the public hostname count only from `TRUSTED_PROXIES`;
  break-glass failures capped at 10 per address and 50 in total per 15 minutes; bad Content-Length
  refused; 30-second connection timeout and at most 64 connections; optional `unifi.cert_sha256`
  certificate pinning; plain error sentences (details in the server log); calendar feed must be https://
  and at most 5 MB; container drops all capabilities with pid and memory limits; admin sign-ins shown as
  `admin:NAME`; deploy block restores the old service on every failure.
- `web.password`, `web.bind`, `web.port` and `db_path` removed from `config.json` (ignored with a warning).
