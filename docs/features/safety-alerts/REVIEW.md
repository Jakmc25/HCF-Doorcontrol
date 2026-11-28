# Review: safety fixes and Telegram alerts, PR 1 (Door Control 1.1.0)

## Round 1 (2026-11-28)
- Code review: CHANGES REQUIRED.
- QA: CHANGES REQUIRED (192 tests pass).
- Security: CHANGES REQUIRED.

Verified fixed: audit findings S1, S2, S4, S5, S8, S9, S10, and every QA audit repro.

### BLOCKER
- **R1** `engine.py:615-626` `_door_tick`: overlapping schedule and calendar windows can still lock a door mid-event.
  - **Problem:** when the two windows overlap, the door's state is saved as source "schedule". If the feed goes stale, or the app restarts before the first fetch, the calendar window drops out. `ends_at` is then lowered to the schedule's end, and the door is reset partway through the event. Example: a schedule 08-17 and an event 16-20 on the same door, with the feed stale at 16:45, resets the door at 17:01. This breaks AC14-16.
  - **Fix:** never lower a stored end time while the calendar isn't trusted, and track the calendar's end separately.
  - **Tests:** cover both the stale-feed and the startup cases.

### SHOULD-FIX
- **Q1** `engine.py` `_end_window` and `_hand_check`: an empty or null `lock_rule` answer is treated as a hand change.
  - **Problem:** the door stays unlocked, nothing retries, and the alert wrongly says the door was changed by hand.
  - **Fix:** only a real, different rule counts as a hand change. An empty or unclear answer retries and alerts "could not read the door".
- **S11** `ical.py:650` and `ical.py:492`: the 24-hour cap is per occurrence.
  - **Problem:** a daily all-day RRULE keeps doors unlocked indefinitely, and `max_window_hours` has no upper limit.
  - **Fix:** refuse back-to-back calendar windows that would hold a door with no break for more than 24 h, and clamp the config value to 24 or less.
- **S12** README lines 358-374 and `ical.py:638-641`: the organizer allowlist only proves what the feed says; the real control is who can edit the calendar.
  - **Fix:** say this in the README, and send an alert when a calendar window opens, with the title and UID.
- **R2** `config.example.json:22`: `"allowed_organizers": []` counts as set, which disables the admin fallback.
  - **Fix:** remove the key from the example and explain it in the README.

### NIT
- **R3:** any buffer above 0 makes every all-day event longer than the cap. Leave buffers out of the cap for all-day events.
- **R4 / Q5:** a DAILY event that started more than about 13 years ago is refused. Document it.
- **R5:** the deploy block refuses to run when the app isn't running, which is exactly the recovery case. `restore_prev` has no health wait. Allow the deploy in that case and add the wait.
- **S13:** the `deploy-*` and `pre-rollback-*` backup folders are never pruned. Keep the newest 10.
- **S14:** the rollback block accepts `../` in the typed name. Validate it against `^deploy-[0-9-]+$`.
- **S15:** refusal alerts repeat the event title and organizer. Strip URLs from titles.
- **S16:** the Telegram per-chat rate limit runs before the allowed-chat check. Check the chat first.
- **S17:** `DOORCONTROL_DEV_ALLOW_HTTP` is logged but not alerted. Send an alert at startup.
- **Q2:** a UniFi outage at the end of a window writes no activity-log row. Log one row per outage.
- **Q3:** a feed that starts with a UTF-8 BOM is rejected. Strip the BOM.
- **Q4:** the 20-per-minute cap counts alerts, not sends, so 2 chats get 40 sends. This matches Jake's decision 9; document it and change nothing.

## Fix round 1
Sent to developer: R1, Q1, S11, S12, R2, R3, R4/Q5 (docs), R5, S13, S14, S15, S16, S17, Q2, Q3, Q4 (docs).

## Re-check (after 7d4bc71): PASS
Code reviewer (opus) verified R1, Q1, S11, S12 and R2 fixed, with no new findings, and accepted the three deviations: (a) the chain cap fails closed and is alerted; (b) the calendar-open alert is low priority, but the action is still logged; (c) unclear reads alert after `failures_before_alert`. No BLOCKER or SHOULD-FIX open.
Release check: NO-GO only on (1) this missing sign-off and (2) the undocumented calendar organizer setup. Both are fixed in docs: the CLAUDE.md deploy notes cover the calendar organizer, and test_qa.py was added to the test commands. No calendar feed is configured on fair-server today, so behavior there is unchanged.
