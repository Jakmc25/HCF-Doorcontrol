# Fairgrounds Door Control

A friendly front end for UniFi Access: one-tap lock/unlock, repeating and one-time schedules,
Google Calendar-driven unlocks, and a Telegram bot. Python 3.9+, **no packages to install**.

It works through UniFi's *temporary lock rules*: when a schedule starts the door gets `keep_unlock`;
when it ends the door goes back to its normal UniFi schedule (`reset`) or is locked (`keep_lock`).
It never edits your UniFi schedules or policies.

## 1. Get a UniFi Access API token
1. Open UniFi (Applications > **Access**) > Settings > Security > **Advanced** > API Token.
2. **Create New**, name it "Door Control", pick a long validity, and give it the **space** permissions
   (view + edit: doors and lock rules).
3. Copy the token. Note the console's local IP; the API is `https://<ip>:12445`.
4. Test from the fair server:
   `curl -k -H "Authorization: Bearer TOKEN" https://<ip>:12445/api/v1/developer/doors`
   You should see your doors as JSON.

## 2. Install on the fair server
```
sudo mkdir -p /srv/fair-apps/door-control && sudo chown office: /srv/fair-apps/door-control
# copy this folder's contents there, then:
cd /srv/fair-apps/door-control
cp config.example.json config.json && chmod 600 config.json   # edit it: host, token, web.password
python3 run.py config.json                                     # try it, open http://<server>:8480
sudo cp door-control.service /etc/systemd/system/
sudo systemctl enable --now door-control
```
The dashboard asks for a password (any username, password from `web.password`).
Keep it on your local network; don't expose it to the internet.

## 3. Google Calendar (optional)
In Google Calendar > the calendar's settings > Integrate calendar > **Secret address in iCal format**.
Paste it into `calendar.ical_url`. Then create events titled like:

- `Unlock: Event Center` - unlocks that door for the event's time
- `Unlock: Event Center, Main Gate` - several doors
- `Unlock: ALL` - every door

Door names only need to match part of the UniFi door name. Recurring events work. Use
`buffer_before_min` / `buffer_after_min` to unlock early or stay open after (e.g. rental setup/cleanup).
Anyone who can edit that calendar can unlock doors, so share it carefully.

## 4. Telegram (optional)
Create a **new** bot with @BotFather (don't reuse FairSync's token; two programs can't poll one bot).
Put its token in `telegram.token`, restart, message the bot: it replies with your chat ID.
Add that to `allowed_chat_ids`, restart. Try `/help`.

## Using it away from the fairgrounds
Never port-forward the dashboard to the internet: it controls door locks and uses a single shared password.
Use one of these instead:

- **Telegram bot** (section 4): works from anywhere with no network setup, since the server only makes outbound calls.
- **Tailscale** (free for personal use): install it on the fair server and on your phone/laptop, sign in to the same account, then
  open `http://<server-tailscale-name>:8480`. Traffic is encrypted end to end and nothing is exposed publicly.
  For a proper https address, set `web.bind` to `127.0.0.1` and run `sudo tailscale serve --bg 8480`; it prints a
  `https://<name>.<tailnet>.ts.net` URL that only your devices can reach. Share the tailnet with anyone else who needs access
  (e.g. via Tailscale's invite feature); each person still needs the dashboard password.

## Notes and limits
- Lock rules need a UniFi Access hub that supports them (API v1.24.6+). `Lock now` uses `lock_now`.
- "Unlock for…" is capped at 8 hours (UniFi custom rule); use a schedule or "Keep unlocked" for longer.
- If someone changes a door by hand during a schedule, the scheduler leaves their change alone at the end.
- If UniFi is unreachable the scheduler keeps retrying every 30 seconds and catches up on restart.
- Test safely without touching real doors: `python3 mock_unifi.py 12445` (token `test-token`,
  host `http://127.0.0.1:12445`). Run tests: `python3 tests/test_all.py`.
