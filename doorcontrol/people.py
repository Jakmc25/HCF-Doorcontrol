"""Who counts as an admin, and who may create calendar unlock events.

Reads the environment only (and, from 1.2.0, the in-app people list). Never imports web,
auth or google_signin, so the scheduler and calendar work with nobody signed in.
"""

import os


def admin_emails(env=None):
    """The admins: the exact emails in GOOGLE_ALLOWED_EMAILS (lower-cased)."""
    env = os.environ if env is None else env
    raw = env.get("GOOGLE_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def allowed_organizers(cal_cfg, env=None):
    """Calendar organizers whose 'Unlock:' events may open doors.

    `calendar.allowed_organizers` in config.json, when present, is the whole list.
    Otherwise the admins in GOOGLE_ALLOWED_EMAILS. Empty means no event opens a door.
    """
    cal_cfg = cal_cfg or {}
    if "allowed_organizers" in cal_cfg:
        vals = cal_cfg.get("allowed_organizers") or []
        if isinstance(vals, str):
            vals = vals.split(",")
        return {str(e).strip().lower() for e in vals if str(e).strip()}
    return admin_emails(env)
