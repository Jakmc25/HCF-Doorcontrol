import json
import logging
import os
import sys
from zoneinfo import ZoneInfo

from .db import DB
from .engine import Engine
from .ical import Calendar
from .telegram_bot import Bot
from .unifi import UnifiClient
from .web import make_server


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

    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "doorcontrol.db")
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    client = UnifiClient(u["host"], u["token"], u.get("verify_tls", False))
    cal_cfg = cfg.get("calendar", {})
    calendar = Calendar(cal_cfg, tz) if cal_cfg.get("ical_url") else None
    engine = Engine(cfg, DB(db_path), client, calendar)

    if calendar:
        calendar.start()
    engine.start(int(cfg.get("tick_seconds", 30)))
    Bot(cfg.get("telegram", {}), engine).start()

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
