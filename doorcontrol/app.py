import json
import logging
import os
import sys
import urllib.parse
from zoneinfo import ZoneInfo

from . import backup, people
from .db import DB
from .engine import Engine
from .ical import Calendar, check_url
from .notify import Notifier
from .telegram_bot import Bot
from .unifi import UnifiClient
from .web import make_server

PIN_HELP = "See README, 'Pin the UniFi console certificate'."


def check_unifi(u, env=None):
    """Refuse to start (before anything is sent) unless the console is reached over https
    and its certificate is pinned (cert_sha256) or verified (verify_tls: true).

    Local development only: DOORCONTROL_DEV_ALLOW_HTTP=1 with http://127.0.0.1 or
    http://localhost (the fake controller, mock_unifi.py). Never set in compose.yaml.
    """
    env = os.environ if env is None else env
    host = str(u.get("host") or "")
    parts = urllib.parse.urlsplit(host)
    if (
        env.get("DOORCONTROL_DEV_ALLOW_HTTP") == "1"
        and parts.scheme == "http"
        and parts.hostname in ("127.0.0.1", "localhost")
    ):
        return "development"
    if parts.scheme != "https":
        raise SystemExit(
            "unifi.host in config.json must start with https:// (the console's API is "
            "https://<console address>:12445). Door Control did not start and sent "
            "nothing to the console."
        )
    if not u.get("cert_sha256") and u.get("verify_tls") is not True:
        raise SystemExit(
            "unifi.cert_sha256 is not set in config.json, so the UniFi console cannot be "
            "checked. Door Control did not start and sent nothing to the console. "
            + PIN_HELP
        )
    return "pinned" if u.get("cert_sha256") else "verified"


def warn_no_organizers(cal_cfg, notifier, env=None, db=None):
    """AC3: a feed is set up but nobody may create unlock events -> one startup alert."""
    if cal_cfg.get("ical_url") and not people.allowed_organizers(cal_cfg, env, db):
        notifier.alert(
            "calendar",
            "A calendar feed is set up, but nobody is allowed to create unlock events "
            "(calendar.allowed_organizers, GOOGLE_ALLOWED_EMAILS and the People list "
            "are all empty). Calendar events open no doors.",
            key=("calendar", "no-organizers"),
        )
        return True
    return False


def warn_dev_mode(mode, notifier):
    """Review S17: plain http to the console is for a local fake controller only; if it
    is ever switched on, the alert chats hear about it at startup, not just the log."""
    if mode != "development":
        return False
    notifier.alert(
        "scheduler",
        "Door Control started with DOORCONTROL_DEV_ALLOW_HTTP=1: it talks to the door "
        "console over plain http without a certificate check. This is for testing "
        "with the fake controller only; remove it from the server's settings.",
        key=("startup", "dev-http"),
    )
    return True


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    # DATA_DIR holds config.json and the database (/data in Docker, data/ locally).
    data_dir = os.environ.get("DATA_DIR", "data")
    path = (
        argv[0]
        if argv
        else os.environ.get("DOORCONTROL_CONFIG")
        or os.path.join(data_dir, "config.json")
    )
    with open(path) as f:
        cfg = json.load(f)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log = logging.getLogger("doorcontrol")

    # Sign-in replaced the dashboard password; DATA_DIR replaced db_path.
    obsolete = [k for k in ("web", "db_path") if k in cfg]
    if obsolete:
        log.warning(
            "config.json: %s no longer used and ignored (sign-in replaced the "
            "password; DATA_DIR sets the data folder).",
            ", ".join(obsolete),
        )
    u = cfg["unifi"]
    if not u.get("token") or u["token"].startswith("PASTE"):
        sys.exit("Set unifi.token in config.json.")
    mode = check_unifi(u)
    if mode == "development":
        log.warning("DOORCONTROL_DEV_ALLOW_HTTP: plain http to a local fake console.")

    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "doorcontrol.db")
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    try:
        client = UnifiClient(
            u["host"],
            u["token"],
            u.get("verify_tls", False),
            cert_sha256=u.get("cert_sha256") or None,
        )
    except ValueError as e:
        sys.exit(str(e))
    cal_cfg = cfg.get("calendar", {})
    try:
        check_url(cal_cfg.get("ical_url"))
    except ValueError as e:
        sys.exit(str(e))

    tg = cfg.get("telegram", {})
    notifier = Notifier(
        tg.get("token", ""),
        tg.get("allowed_chat_ids", []),
        cfg.get("alerts", {}),
        secrets=[u["token"], cal_cfg.get("ical_url") or ""],
    )
    notifier.start()
    if not notifier.enabled:
        log.info("Telegram alerts are off (no bot token or no private allowed chats).")

    db = DB(db_path)
    # Organizers default to the admins plus the in-app People list, read at each refresh.
    calendar = (
        Calendar(
            cal_cfg,
            tz,
            notifier=notifier,
            organizers=lambda: people.allowed_organizers(cal_cfg, db=db),
        )
        if cal_cfg.get("ical_url")
        else None
    )
    warn_no_organizers(cal_cfg, notifier, db=db)
    warn_dev_mode(mode, notifier)
    engine = Engine(cfg, db, client, calendar, notifier)

    if calendar:
        calendar.start()
    engine.start(int(cfg.get("tick_seconds", 30)))
    Bot(tg, engine).start()
    backup.start(data_dir, path, notifier)

    bind = os.environ.get("BIND", "0.0.0.0")
    port = int(os.environ.get("PORT", "8480"))
    server = make_server(engine, data_dir, bind, port)
    log.info("Door Control running on http://%s:%s", bind, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    main()
