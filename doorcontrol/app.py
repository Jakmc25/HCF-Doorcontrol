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
    path = argv[0] if argv else os.environ.get("DOORCONTROL_CONFIG", "config.json")
    with open(path) as f:
        cfg = json.load(f)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log = logging.getLogger("doorcontrol")

    web_cfg = cfg.get("web", {})
    if not web_cfg.get("password") or web_cfg["password"] == "change-me":
        sys.exit("Set web.password in config.json (it protects the lock controls).")
    u = cfg["unifi"]
    if not u.get("token") or u["token"].startswith("PASTE"):
        sys.exit("Set unifi.token in config.json.")

    db_path = cfg.get("db_path", "data/doorcontrol.db")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    client = UnifiClient(u["host"], u["token"], u.get("verify_tls", False))
    cal_cfg = cfg.get("calendar", {})
    calendar = Calendar(cal_cfg, tz) if cal_cfg.get("ical_url") else None
    engine = Engine(cfg, DB(db_path), client, calendar)

    if calendar:
        calendar.start()
    engine.start(int(cfg.get("tick_seconds", 30)))
    Bot(cfg.get("telegram", {}), engine).start()

    server = make_server(web_cfg, engine)
    log.info(
        "Door Control running on http://%s:%s",
        web_cfg.get("bind", "0.0.0.0"),
        web_cfg.get("port", 8480),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    main()
